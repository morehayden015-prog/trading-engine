"""
auto_calibrate.py — Automatic calibration scheduler
Runs calibration:
  - Every Sunday at 00:00 UTC
  - Every 25 new labelled trades
Sends calibration report to #bot-updates Discord channel.
"""

import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from calibrate import calibrate
from strategy_manager import StrategyManager
from self_learning import run_self_learning_cycle

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "trades.db")

_last_trade_count: int = 0
TRADES_TRIGGER = int(os.getenv("CAL_TRADES_TRIGGER", "25"))


def _get_labelled_count() -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT COUNT(*) as n FROM paper_trades WHERE result IS NOT NULL").fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0


async def run_calibration_cycle():
    """Full calibration + strategy management run. Call this from the scheduler."""
    log.info("Auto-calibration starting...")

    # Weight calibration
    result = calibrate()

    # Strategy auto-management
    sm = StrategyManager()
    report = sm.run_auto_management()

    # Self-learning cycle
    run_self_learning_cycle(trigger="weekly")

    sm.close()

    # Compose Discord message
    try:
        from alerts import send_bot_update
        n        = result.get("n_outcomes", 0)
        wr       = result.get("win_rate", 0)
        changes  = result.get("changes", {})
        disabled = report.get("disabled", [])
        enabled  = report.get("enabled", [])

        lines = [
            f"**Auto-Calibration Complete**",
            f"Trades analyzed: {n} | Win rate: {wr:.1%}",
            f"Weight changes: {len(changes)}",
        ]

        if changes:
            for comp, ch in changes.items():
                arrow = "▲" if ch["diff"] > 0 else "▼"
                lines.append(f"  {arrow} {comp}: {ch['old']} → {ch['new']}")

        if disabled:
            lines.append(f"\n⚠️ Auto-disabled: {', '.join(disabled)}")
        if enabled:
            lines.append(f"✅ Re-enabled: {', '.join(enabled)}")

        await send_bot_update("Weekly Calibration Report", "\n".join(lines))
    except Exception as e:
        log.error(f"Failed to send calibration Discord update: {e}")

    log.info("Auto-calibration complete")
    return result


async def calibration_loop():
    """
    Long-running loop. Checks two triggers:
    1. Sunday 00:00 UTC weekly run
    2. Every 25 new labelled trades
    """
    global _last_trade_count
    _last_trade_count = _get_labelled_count()
    last_sunday = None

    while True:
        await asyncio.sleep(300)  # check every 5 minutes

        now = datetime.now(timezone.utc)

        # Sunday trigger
        if now.weekday() == 6 and now.hour == 0:
            sunday_key = now.strftime("%Y-%W")
            if sunday_key != last_sunday:
                log.info("Sunday calibration trigger")
                await run_calibration_cycle()
                last_sunday = sunday_key

        # Trade count trigger
        current_count = _get_labelled_count()
        if current_count - _last_trade_count >= TRADES_TRIGGER:
            log.info(f"Trade count trigger: {current_count} trades")
            await run_calibration_cycle()  # already calls run_self_learning_cycle internally
            _last_trade_count = current_count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(calibration_loop())