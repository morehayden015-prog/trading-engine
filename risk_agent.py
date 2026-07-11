"""
risk_agent.py — Portfolio Risk Management Agent
Runs every 60 seconds. Monitors:
  - Drawdown curve (daily/weekly P&L)
  - Position correlation (e.g. long ES + long NQ = correlated risk)
  - Consecutive losses
  - Dynamic sizing multiplier

Updates AgentContext. Called by main.py before trade execution.
"""
import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from agent_context import context

log = logging.getLogger(__name__)

DB_PATH      = os.getenv("DB_PATH", "trades.db")
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "10000"))

# Correlated market groups — being long/short in same group = correlated risk
CORRELATED_GROUPS = [
    {"ES", "NQ"},           # US equities
    {"XAUUSD"},             # Gold (standalone)
    {"CL"},                 # Oil (standalone)
]


def _get_pnl(days: int) -> float:
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conn  = sqlite3.connect(DB_PATH)
        row   = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status='CLOSED' AND exit_time >= ?",
            (since,),
        ).fetchone()
        conn.close()
        return round(row[0] or 0.0, 2)
    except Exception:
        return 0.0


def _get_consecutive_losses() -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT result FROM paper_trades WHERE status='CLOSED' AND result IS NOT NULL ORDER BY rowid DESC LIMIT 10"
        ).fetchall()
        conn.close()
        streak = 0
        for r in rows:
            if r[0] == "LOSS":
                streak += 1
            else:
                break
        return streak
    except Exception:
        return 0


def _get_open_positions() -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT symbol, direction FROM paper_trades WHERE status='OPEN'"
        ).fetchall()
        conn.close()
        return [{"symbol": r[0], "direction": r[1]} for r in rows]
    except Exception:
        return []


def check_correlation_risk(symbol: str, direction: str, open_positions: list) -> dict:
    """
    Check if adding this trade creates excessive correlated exposure.
    Returns {"allowed": bool, "reason": str, "correlated_count": int}
    """
    correlated = []
    for pos in open_positions:
        for group in CORRELATED_GROUPS:
            if symbol in group and pos["symbol"] in group:
                correlated.append(pos)
                break

    same_direction = [p for p in correlated if p["direction"].upper() == direction.upper()]

    if len(same_direction) >= 2:
        return {
            "allowed":          False,
            "reason":           f"Too much correlated exposure: {len(same_direction)} same-direction positions in correlated markets",
            "correlated_count": len(same_direction),
        }

    return {
        "allowed":          True,
        "reason":           "Correlation check passed",
        "correlated_count": len(same_direction),
    }


def calc_dynamic_sizing(
    daily_pnl:    float,
    weekly_pnl:   float,
    consecutive:  int,
    vix_mult:     float,
    regime:       str,
) -> float:
    """
    Calculate position sizing multiplier based on current risk state.
    Base = 1.0. Range = 0.25–1.5.
    """
    mult = 1.0

    # Drawdown scaling
    daily_pct  = daily_pnl  / ACCOUNT_SIZE * 100
    weekly_pct = weekly_pnl / ACCOUNT_SIZE * 100

    if daily_pct < -2.0:
        mult *= 0.5     # down >2% today — halve size
    elif daily_pct < -1.0:
        mult *= 0.75    # down 1-2% — reduce
    elif daily_pct > 1.5:
        mult *= 1.1     # up >1.5% — slight boost

    if weekly_pct < -5.0:
        mult *= 0.5
    elif weekly_pct < -3.0:
        mult *= 0.75

    # Consecutive loss penalty
    if consecutive >= 3:
        mult *= 0.5
    elif consecutive == 2:
        mult *= 0.75

    # VIX-based adjustment (from intel_agent)
    mult *= vix_mult

    # Regime adjustment
    if regime == "VOLATILE":
        mult *= 0.7
    elif regime == "TRANSITIONING":
        mult *= 0.8
    elif regime == "TRENDING_BULL" or regime == "TRENDING_BEAR":
        mult *= 1.1

    return round(min(1.5, max(0.25, mult)), 2)


async def risk_agent_loop():
    """
    Background agent — updates risk state every 60 seconds.
    """
    log.info("Risk agent started")

    while True:
        try:
            daily_pnl    = _get_pnl(days=1)
            weekly_pnl   = _get_pnl(days=7)
            consecutive  = _get_consecutive_losses()
            open_pos     = _get_open_positions()
            vix_mult     = context.get("sizing_multiplier", 1.0)
            regime       = context.get("regime", "UNKNOWN")

            sizing = calc_dynamic_sizing(
                daily_pnl=daily_pnl,
                weekly_pnl=weekly_pnl,
                consecutive=consecutive,
                vix_mult=vix_mult,
                regime=regime,
            )

            await context.update(
                daily_pnl=daily_pnl,
                weekly_pnl=weekly_pnl,
                consecutive_losses=consecutive,
                open_count=len(open_pos),
                sizing_multiplier=sizing,
            )

            log.debug(
                f"Risk update | daily={daily_pnl:+.2f} weekly={weekly_pnl:+.2f} "
                f"streak={consecutive} sizing={sizing}x"
            )

            # Alert if sizing drops severely
            if sizing <= 0.4 and consecutive >= 3:
                try:
                    from alerts import send_bot_update
                    await send_bot_update(
                        "🚨 Risk Alert — Reduced Sizing",
                        f"**Consecutive Losses:** {consecutive}\n"
                        f"**Daily P&L:** ${daily_pnl:+.2f}\n"
                        f"**Weekly P&L:** ${weekly_pnl:+.2f}\n"
                        f"**Position Sizing:** {sizing}x base risk\n\n"
                        f"Bot is in risk-reduction mode.",
                    )
                except Exception:
                    pass

        except Exception as e:
            log.error(f"Risk agent error: {e}")

        await asyncio.sleep(60)
