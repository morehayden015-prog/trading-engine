"""
auto_labeler.py — Automatic trade outcome labeler
Runs every 60 seconds, fetches live prices via yfinance,
checks TP/SL levels, auto-closes trades and sends Discord alerts.

TP targeting is dynamic based on strategy win rate:
  Win rate < 45%  → TP1 only (conservative)
  Win rate 45-60% → TP2 (standard)
  Win rate > 60%  → TP3 (let winners run)
"""
import asyncio
import logging
import os
import sqlite3

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "trades.db")

# yfinance ticker map
SYMBOL_MAP = {
    "XAUUSD": "GC=F",
    "ES":     "ES=F",
    "NQ":     "NQ=F",
    "CL":     "CL=F",
}

# TP distances per symbol (price units)
TP_DISTANCES = {
    "XAUUSD": {"TP1": 5.0,  "TP2": 10.0, "TP3": 15.0, "SL": 4.0},
    "ES":     {"TP1": 5.0,  "TP2": 10.0, "TP3": 20.0, "SL": 6.0},
    "NQ":     {"TP1": 15.0, "TP2": 30.0, "TP3": 60.0, "SL": 20.0},
    "CL":     {"TP1": 0.30, "TP2": 0.60, "TP3": 1.00, "SL": 0.25},
}


def _get_current_price(symbol: str) -> float | None:
    try:
        import yfinance as yf
        ticker = SYMBOL_MAP.get(symbol)
        if not ticker:
            return None
        data = yf.Ticker(ticker).fast_info
        price = data.get("lastPrice") or data.get("regularMarketPrice")
        return float(price) if price else None
    except Exception as e:
        log.warning(f"Price fetch failed for {symbol}: {e}")
        return None


def _get_strategy_win_rate(strategy: str, symbol: str) -> float | None:
    """Fetch win rate for a strategy/symbol combo from the DB."""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins
               FROM paper_trades
               WHERE strategy=? AND symbol=? AND result IS NOT NULL""",
            (strategy, symbol),
        ).fetchone()
        conn.close()
        total, wins = row[0], row[1] or 0
        return round(wins / total, 3) if total >= 5 else None
    except Exception:
        return None


def _choose_tp(win_rate: float | None) -> str:
    """
    Choose which TP to target based on win rate.
    No data yet → TP1 (safe default)
    < 45%       → TP1 (underperforming, take quick profits)
    45–60%      → TP2 (standard)
    > 60%       → TP3 (strong edge, let winners run)
    """
    if win_rate is None or win_rate < 0.45:
        return "TP1"
    elif win_rate < 0.60:
        return "TP2"
    else:
        return "TP3"


def _check_trade(trade: dict, current_price: float) -> str | None:
    """
    Check a single trade against its dynamic TP and SL.
    Returns 'WIN', 'LOSS', or None (still open).
    """
    symbol      = trade["symbol"]
    direction   = trade["direction"].upper()
    entry_price = trade["entry_price"]
    strategy    = trade["strategy"]

    levels   = TP_DISTANCES.get(symbol, TP_DISTANCES["XAUUSD"])
    win_rate = _get_strategy_win_rate(strategy, symbol)
    tp_key   = _choose_tp(win_rate)
    tp_dist  = levels[tp_key]
    sl_dist  = levels["SL"]

    log.debug(
        f"{trade['trade_id']} | WR={win_rate} → targeting {tp_key} "
        f"(+{tp_dist}) SL(-{sl_dist})"
    )

    if direction in ("BUY", "LONG"):
        if current_price >= entry_price + tp_dist:
            return "WIN"
        elif current_price <= entry_price - sl_dist:
            return "LOSS"
    else:  # SELL / SHORT
        if current_price <= entry_price - tp_dist:
            return "WIN"
        elif current_price >= entry_price + sl_dist:
            return "LOSS"

    return None


async def auto_label_loop(executor, labeler):
    """
    Background loop — checks open trades every 60s,
    auto-labels WIN/LOSS when dynamic TP or SL is hit.
    """
    log.info("Auto-labeler started (dynamic TP targeting)")

    while True:
        await asyncio.sleep(60)

        try:
            open_trades = executor.get_open_trades()
            if not open_trades:
                continue

            symbols = set(t["symbol"] for t in open_trades)
            prices  = {}
            for symbol in symbols:
                p = _get_current_price(symbol)
                if p:
                    prices[symbol] = p

            for trade in open_trades:
                symbol = trade["symbol"]
                price  = prices.get(symbol)
                if price is None:
                    continue

                result = _check_trade(trade, price)
                if result is None:
                    continue

                trade_id = trade["trade_id"]
                executor.close_trade(trade_id, result, exit_price=price)
                labeler.label_manual(trade_id, result, exit_price=price)

                log.info(f"Auto-labeled: {trade_id} → {result} @ {price}")

                # Discord notification
                try:
                    from alerts import send_trade_closed
                    conn = sqlite3.connect(DB_PATH)
                    row  = conn.execute(
                        "SELECT pnl, strategy, risk_pct, risk_dollars FROM paper_trades WHERE trade_id=?", (trade_id,)
                    ).fetchone()
                    conn.close()
                    pnl      = row[0] if row and row[0] is not None else 0.0
                    strategy = row[1] if row else trade.get("strategy", "?")
                    risk_pct = row[2] if row else None
                    risk_usd = row[3] if row else None

                    win_rate = _get_strategy_win_rate(strategy, symbol)
                    tp_used  = _choose_tp(win_rate)

                    await send_trade_closed(
                        trade_id=trade_id,
                        symbol=symbol,
                        result=result,
                        exit_price=price,
                        pnl=pnl,
                        tp_used=tp_used,
                        win_rate=win_rate,
                        risk_pct=risk_pct,
                        risk_usd=risk_usd,
                    )
                except Exception as e:
                    log.error(f"Discord alert failed for {trade_id}: {e}")

        except Exception as e:
            log.error(f"Auto-labeler error: {e}")
