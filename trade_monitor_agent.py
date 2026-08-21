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

# Levels and tickers come from levels.py — the one place they're defined.
from levels import SYMBOL_MAP, TP_DISTANCES  # noqa: E402  (re-exported for callers)


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


# A target below this many R is never selected, whatever the win rate says.
MIN_TARGET_R = float(os.getenv("MIN_TARGET_R", "1.5"))


def _choose_tp(win_rate: float | None, regime: str, symbol: str = None) -> str:
    """
    Choose TP target based on win rate AND current market regime, subject to
    a floor on the reward:risk of the target actually chosen.

    Trending regime = more aggressive targets.

    The win-rate rule alone dropped to TP1 below 45% — "underperforming, take
    quick profits". On most symbols TP1 sits inside 1R (NQ: 15pt TP1 against a
    20pt stop = 0.75R), and a sub-1R target needs a HIGHER win rate to break
    even, not a lower one: NQ at TP1 breaks even at 57.1%, ES at 54.5%. So the
    rule fired on weakness and then demanded a win rate the strategy had just
    shown it did not have, with no way back — the win rate could not recover
    while the target guaranteed negative expectancy, and the target would not
    widen while the win rate stayed low.

    The regime boost was the only escape hatch, and it is currently welded
    shut: regime classification has been failing (Anthropic API credit
    balance), so `regime` is stuck at "UNKNOWN" and regime_boost is never
    True. Every strategy under 45% has therefore been pinned to TP1.

    Now the win rate and regime still pick the *preferred* target, but any
    target under MIN_TARGET_R is skipped in favour of the next one out.
    """
    # Regime boost: trending = aim higher
    regime_boost = regime in ("TRENDING_BULL", "TRENDING_BEAR")

    if win_rate is None:
        preferred = "TP2" if regime_boost else "TP1"
    elif win_rate >= 0.60:
        preferred = "TP3"
    elif win_rate >= 0.45:
        preferred = "TP3" if regime_boost else "TP2"
    else:
        preferred = "TP2" if regime_boost else "TP1"

    levels = TP_DISTANCES.get(symbol, TP_DISTANCES["XAUUSD"]) if symbol else None
    if not levels or not levels.get("SL"):
        return preferred

    order = ["TP1", "TP2", "TP3"]
    sl    = levels["SL"]
    for key in order[order.index(preferred):]:
        if levels.get(key) and levels[key] / sl >= MIN_TARGET_R:
            return key

    # Nothing clears the floor — take the widest target available.
    return order[-1]


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
            "SELECT symbol, direction, entry_price, risk_pct, risk_dollars, rr, "
            "COALESCE(tp1_hit, 0) as tp1_hit, "
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

        # P&L is measured against the price the leg actually exited at,
        # divided by the stop distance — NOT against the planned rr. Booking
        # `risk_usd * rr` for a win meant a trade labelled at the TP1 distance
        # (which _choose_tp targets whenever win rate < 45%) was still paid its
        # full 1.5-3.0R plan, and every loss booked exactly -1R however far
        # price had actually gapped past the stop.
        levels   = TP_DISTANCES.get(row["symbol"]) or {}
        sl_dist  = levels.get("SL")
        tp1_dist = levels.get("TP1")
        sign     = 1 if str(row["direction"]).upper() in ("BUY", "LONG") else -1

        if partial:
            # First (TP1) partial close — banks half the position at the
            # TP1 distance, which is TP1/SL in R terms, not rr/2.
            if sl_dist and tp1_dist:
                pnl = round((risk_usd / 2) * (tp1_dist / sl_dist), 2)
            else:
                pnl = round(risk_usd * (rr / 2), 2)
                log.warning(f"{trade_id}: unknown stop distance for {row['symbol']} — "
                            f"partial booked at planned RR ({pnl:+.2f}), figure is synthetic")
        else:
            # Final close of the runner leg. If TP1 already partialed out,
            # only half the original risk is still open for this leg.
            remaining_risk = risk_usd / 2 if already_partial else risk_usd
            if result == "BE":
                leg_pnl = 0.0
            elif sl_dist and exit_price is not None:
                r_real  = ((exit_price - row["entry_price"]) * sign) / sl_dist
                leg_pnl = round(remaining_risk * r_real, 2)
            else:
                leg_pnl = round(remaining_risk * rr, 2) if result == "WIN" else round(-remaining_risk, 2)
                log.warning(f"{trade_id}: no exit price or unknown stop distance for "
                            f"{row['symbol']} — leg booked at planned RR ({leg_pnl:+.2f}), "
                            f"figure is synthetic")
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
                tp_target = _choose_tp(win_rate, regime, symbol)

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

                # 0. Stop loss hit — checked BEFORE any target. This loop polls
                # every 30s, so a price sitting past both the stop and a target
                # says nothing about which was touched first. Evaluating targets
                # first (the previous order) silently resolved every one of
                # those ambiguous polls as a win. Taking the loss is the
                # standard conservative convention.
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
                    continue

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

                # (stop-loss handling now runs first — see step 0 above)

        except Exception as e:
            log.error(f"Trade monitor error: {e}")
