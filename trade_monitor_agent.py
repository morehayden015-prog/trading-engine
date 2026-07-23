"""
trade_monitor_agent.py — Active Trade Monitor Agent
Replaces simple auto_labeler with intelligent trade management.

Every 30 seconds:
  1. Fetches live prices for all open positions
  2. Moves stop to breakeven after 1:1 R is hit
  3. Scales out (partial WIN) at TP1 when targeting TP2/TP3
  4. Closes full position at final TP or SL
  5. Trails stop in trending regime

TP targeting is dynamic based on strategy win rate (from auto_labeler logic).
"""
import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone

import yfinance as yf

from agent_context import context

log = logging.getLogger(__name__)

DB_PATH      = os.getenv("DB_PATH", "trades.db")
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "10000"))

SYMBOL_MAP = {
    "XAUUSD": "GC=F",
    "ES":     "ES=F",
    "NQ":     "NQ=F",
    "CL":     "CL=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
}

# TP/SL distances per symbol (price units)
TP_DISTANCES = {
    "XAUUSD": {"TP1": 5.0,  "TP2": 10.0, "TP3": 15.0, "SL": 4.0},
    "ES":     {"TP1": 5.0,  "TP2": 10.0, "TP3": 20.0, "SL": 6.0},
    "NQ":     {"TP1": 15.0, "TP2": 30.0, "TP3": 60.0, "SL": 20.0},
    "CL":     {"TP1": 0.30, "TP2": 0.60, "TP3": 1.00, "SL": 0.25},
    # Forex majors (price units, i.e. 0.0010 = 10 pips for 4-decimal pairs)
    "EURUSD": {"TP1": 0.0015, "TP2": 0.0030, "TP3": 0.0050, "SL": 0.0012},
    "GBPUSD": {"TP1": 0.0020, "TP2": 0.0040, "TP3": 0.0065, "SL": 0.0016},
    "AUDUSD": {"TP1": 0.0012, "TP2": 0.0025, "TP3": 0.0040, "SL": 0.0010},
    "USDJPY": {"TP1": 0.15,   "TP2": 0.30,   "TP3": 0.50,   "SL": 0.12},
}


def compute_trade_levels(symbol: str, direction: str, entry_price: float,
                          current_sl: float | None = None,
                          be_moved: bool = False) -> dict:
    """
    Pure price-level calculator shared with the dashboard/API display code
    (main.py, paper_executor.py) so TP1/TP2/TP3/SL shown to the user always
    match exactly what this monitor loop is actually managing against —
    no separate/duplicate distance table to drift out of sync.

    Returns absolute price levels, not distances. `sl` reflects breakeven
    once be_moved is true, mirroring the live management logic above.
    """
    levels = TP_DISTANCES.get(symbol, TP_DISTANCES["XAUUSD"])
    is_long = direction.upper() in ("BUY", "LONG")

    if be_moved and current_sl is not None:
        sl = current_sl
    else:
        sl = (entry_price - levels["SL"]) if is_long else (entry_price + levels["SL"])

    sign = 1 if is_long else -1
    return {
        "sl":  round(sl, 5),
        "tp1": round(entry_price + sign * levels["TP1"], 5),
        "tp2": round(entry_price + sign * levels["TP2"], 5),
        "tp3": round(entry_price + sign * levels["TP3"], 5),
    }


def _get_price(symbol: str) -> float | None:
    try:
        ticker = SYMBOL_MAP.get(symbol)
        if not ticker:
            return None
        info  = yf.Ticker(ticker).fast_info
        price = info.get("lastPrice") or info.get("regularMarketPrice")
        return round(float(price), 4) if price else None
    except Exception as e:
        log.warning(f"Price fetch failed for {symbol}: {e}")
        return None


def _get_strategy_win_rate(strategy: str, symbol: str) -> float | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute(
            """SELECT COUNT(*) as total,
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


def _choose_tp(win_rate: float | None, regime: str) -> str:
    """
    Choose TP target based on win rate AND current market regime.
    Trending regime = more aggressive targets.
    """
    # Regime boost: trending = aim higher
    regime_boost = regime in ("TRENDING_BULL", "TRENDING_BEAR")

    if win_rate is None:
        return "TP2" if regime_boost else "TP1"

    if win_rate >= 0.60:
        return "TP3"
    elif win_rate >= 0.45:
        return "TP3" if regime_boost else "TP2"
    else:
        return "TP2" if regime_boost else "TP1"


def _get_open_trades() -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            # PARTIAL trades (TP1 already scaled out, runner still live) must
            # keep being monitored here too, or the runner's remaining size
            # never reaches TP2/TP3/SL and the trade is stuck at status
            # 'PARTIAL' forever — invisible to open-position counts AND to
            # closed-trade analysis.
            """SELECT trade_id, symbol, direction, strategy, entry_price,
                      risk_dollars, rr, score,
                      COALESCE(be_moved, 0) as be_moved,
                      COALESCE(tp1_hit, 0) as tp1_hit,
                      COALESCE(current_sl, entry_price) as current_sl
               FROM paper_trades WHERE status IN ('OPEN', 'PARTIAL')"""
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"DB error fetching open trades: {e}")
        return []


_MANAGEMENT_COLUMNS = [
    "be_moved INTEGER DEFAULT 0",
    "tp1_hit INTEGER DEFAULT 0",
    "current_sl REAL",
    "partial_pnl_banked REAL DEFAULT 0",
]


def _ensure_management_columns(conn):
    for col in _MANAGEMENT_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col}")
        except Exception:
            pass  # column already exists


def _update_trade_meta(trade_id: str, **kwargs):
    """Update trade management columns (BE moved, TP1 hit, trailing SL)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        _ensure_management_columns(conn)

        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [trade_id]
        conn.execute(f"UPDATE paper_trades SET {sets} WHERE trade_id=?", vals)
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"Trade meta update failed: {e}")


def _close_trade_in_db(trade_id: str, result: str, exit_price: float, partial: bool = False):
    """
    Close (or partially close) a trade in the DB and calculate P&L.
    Returns (pnl, risk_pct, risk_dollars).

    A trade that already had its TP1 partial banked (tp1_hit=1) only has
    HALF its original risk still on the table for the runner leg — the
    final pnl must be partial_pnl_banked + the runner leg's own P&L, not
    a fresh full-size calculation (which would silently discard the
    profit/loss already realized at TP1).
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        _ensure_management_columns(conn)
        row = conn.execute(
            "SELECT risk_pct, risk_dollars, rr, COALESCE(tp1_hit, 0) as tp1_hit, "
            "COALESCE(partial_pnl_banked, 0) as partial_pnl_banked "
            "FROM paper_trades WHERE trade_id=?", (trade_id,)
        ).fetchone()

        if not row:
            conn.close()
            return 0.0, None, None

        risk_pct    = row["risk_pct"]
        risk_usd    = row["risk_dollars"]
        rr          = row["rr"]
        already_partial = bool(row["tp1_hit"])
        banked      = row["partial_pnl_banked"] or 0.0

        if partial:
            # First (TP1) partial close — banks half the size's worth of profit.
            pnl = round(risk_usd * (rr / 2), 2)
        else:
            # Final close of the runner leg. If TP1 already partialed out,
            # only half the original risk is still open for this leg.
            remaining_risk = risk_usd / 2 if already_partial else risk_usd
            if result == "WIN":
                leg_pnl = round(remaining_risk * rr, 2)
            elif result == "LOSS":
                leg_pnl = round(-remaining_risk, 2)
            else:
                leg_pnl = 0.0
            pnl = round(banked + leg_pnl, 2)

        status = "PARTIAL" if partial else "CLOSED"
        params = [result, exit_price, datetime.utcnow().isoformat(), pnl, status, trade_id]
        if partial:
            conn.execute(
                "UPDATE paper_trades SET result=?, exit_price=?, exit_time=?, pnl=?, status=?, "
                "partial_pnl_banked=? WHERE trade_id=?",
                params[:-1] + [pnl, trade_id],
            )
        else:
            conn.execute(
                "UPDATE paper_trades SET result=?, exit_price=?, exit_time=?, pnl=?, status=? WHERE trade_id=?",
                params,
            )
        conn.commit()
        conn.close()
        return pnl, risk_pct, risk_usd
    except Exception as e:
        log.error(f"Close trade DB error: {e}")
        return 0.0, None, None


async def trade_monitor_agent_loop():
    """
    Background agent — actively manages all open positions every 30 seconds.
    """
    log.info("Trade monitor agent started")

    # Ensure management columns exist on startup
    try:
        conn = sqlite3.connect(DB_PATH)
        _ensure_management_columns(conn)
        conn.commit()
        conn.close()
    except Exception:
        pass

    while True:
        await asyncio.sleep(30)

        try:
            open_trades = _get_open_trades()
            if not open_trades:
                continue

            regime = context.get("regime", "UNKNOWN")

            # Fetch prices for all unique symbols
            symbols = set(t["symbol"] for t in open_trades)
            prices  = {}
            loop    = asyncio.get_event_loop()
            for sym in symbols:
                p = await loop.run_in_executor(None, _get_price, sym)
                if p:
                    prices[sym] = p

            for trade in open_trades:
                symbol     = trade["symbol"]
                price      = prices.get(symbol)
                if price is None:
                    continue

                trade_id   = trade["trade_id"]
                direction  = trade["direction"].upper()
                entry      = trade["entry_price"]
                strategy   = trade["strategy"]
                be_moved   = bool(trade["be_moved"])
                tp1_hit    = bool(trade["tp1_hit"])
                current_sl = trade["current_sl"] or entry

                levels   = TP_DISTANCES.get(symbol, TP_DISTANCES["XAUUSD"])
                win_rate = _get_strategy_win_rate(strategy, symbol)
                tp_target = _choose_tp(win_rate, regime)

                sl_dist  = levels["SL"]
                tp1_dist = levels["TP1"]
                tp2_dist = levels["TP2"]
                tp3_dist = levels["TP3"]

                target_dist = {"TP1": tp1_dist, "TP2": tp2_dist, "TP3": tp3_dist}[tp_target]

                is_long = direction in ("BUY", "LONG")

                # --- Price levels ---
                if is_long:
                    sl_price = entry - sl_dist
                    tp1      = entry + tp1_dist
                    tp2      = entry + tp2_dist
                    tp3      = entry + tp3_dist
                    target   = entry + target_dist
                    at_be    = price >= tp1          # 1:1 hit → move to BE
                    at_tp1   = price >= tp1
                    at_tp2   = price >= tp2
                    at_final = price >= target
                    at_sl    = price <= (entry if be_moved else sl_price)
                else:  # SHORT
                    sl_price = entry + sl_dist
                    tp1      = entry - tp1_dist
                    tp2      = entry - tp2_dist
                    tp3      = entry - tp3_dist
                    target   = entry - target_dist
                    at_be    = price <= tp1
                    at_tp1   = price <= tp1
                    at_tp2   = price <= tp2
                    at_final = price <= target
                    at_sl    = price >= (entry if be_moved else sl_price)

                # --- Trade management logic ---

                # 1. Move to breakeven after TP1 hit
                if at_be and not be_moved:
                    _update_trade_meta(trade_id, be_moved=1, current_sl=entry)
                    log.info(f"{trade_id} | Stop moved to BE @ {entry}")
                    try:
                        from alerts import send_bot_update
                        await send_bot_update(
                            f"🔒 Stop → Breakeven | {symbol}",
                            f"Trade **{trade_id}** hit TP1 — stop moved to breakeven.\n"
                            f"Targeting **{tp_target}** | WR={win_rate:.0%}" if win_rate else
                            f"Trade **{trade_id}** hit TP1 — stop moved to breakeven.\nTargeting **{tp_target}**",
                        )
                    except Exception:
                        pass
                    continue

                # 2. Partial close at TP1 if targeting TP2 or TP3
                if at_tp1 and not tp1_hit and tp_target in ("TP2", "TP3"):
                    pnl, risk_pct, risk_usd = _close_trade_in_db(trade_id, "WIN", price, partial=True)
                    _update_trade_meta(trade_id, tp1_hit=1, be_moved=1)
                    log.info(f"{trade_id} | Partial close @ TP1 {price} | P&L={pnl:+.2f}")
                    try:
                        from alerts import send_trade_closed
                        await send_trade_closed(
                            trade_id=trade_id,
                            symbol=symbol,
                            result="WIN",
                            exit_price=price,
                            pnl=pnl,
                            tp_used="TP1 (Partial)",
                            win_rate=win_rate,
                            risk_pct=risk_pct,
                            risk_usd=risk_usd,
                        )
                    except Exception:
                        pass
                    continue

                # 3. Full close at final target
                if at_final:
                    pnl, risk_pct, risk_usd = _close_trade_in_db(trade_id, "WIN", price)
                    log.info(f"{trade_id} | WIN @ {tp_target} {price} | P&L={pnl:+.2f}")
                    try:
                        from alerts import send_trade_closed
                        await send_trade_closed(
                            trade_id=trade_id,
                            symbol=symbol,
                            result="WIN",
                            exit_price=price,
                            pnl=pnl,
                            tp_used=tp_target,
                            win_rate=win_rate,
                            risk_pct=risk_pct,
                            risk_usd=risk_usd,
                        )
                    except Exception:
                        pass
                    continue

                # 4. Stop loss hit
                if at_sl:
                    pnl, risk_pct, risk_usd = _close_trade_in_db(trade_id, "LOSS", price)
                    log.info(f"{trade_id} | LOSS @ SL {price} | P&L={pnl:+.2f}")
                    try:
                        from alerts import send_trade_closed
                        await send_trade_closed(
                            trade_id=trade_id,
                            symbol=symbol,
                            result="LOSS",
                            exit_price=price,
                            pnl=pnl,
                            tp_used="SL",
                            win_rate=win_rate,
                            risk_pct=risk_pct,
                            risk_usd=risk_usd,
                        )
                    except Exception:
                        pass

        except Exception as e:
            log.error(f"Trade monitor error: {e}")
