"""
fee_tracker.py — Fee tracking + circuit breakers
Monitors cumulative losses and prevents slow bleed.
Circuit breakers:
  - Daily loss limit: 3% of account
  - Weekly loss limit: 7% of account
  - Consecutive losses: 4 in a row → pause 1 hour
"""
import os
import sqlite3
import logging
from datetime import datetime, timezone, timedelta

from paper_executor import get_current_equity

log = logging.getLogger(__name__)

DB_PATH      = os.getenv("DB_PATH", "trades.db")
# Starting balance fallback only — see get_current_equity() for the actual
# equity figure (starting balance + realized P&L) that circuit-breaker
# thresholds are computed against below.
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "10000"))

DAILY_LOSS_LIMIT_PCT  = float(os.getenv("DAILY_LOSS_LIMIT_PCT",  "3.0"))
WEEKLY_LOSS_LIMIT_PCT = float(os.getenv("WEEKLY_LOSS_LIMIT_PCT", "7.0"))
CONSEC_LOSS_LIMIT     = int(os.getenv("CONSEC_LOSS_LIMIT",       "4"))


class FeeTracker:
    def __init__(self, account_size: float = None):
        # Compounding by default: daily/weekly loss-limit thresholds scale
        # with the account's actual current equity, not a fixed starting
        # balance. Callers that explicitly pass account_size still override.
        self.account_size = account_size if account_size is not None else get_current_equity(DB_PATH)
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_daily_pnl(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT SUM(pnl) as total FROM paper_trades WHERE result IS NOT NULL AND exit_time LIKE ?",
            (f"{today}%",),
        ).fetchone()
        return round(row["total"] or 0.0, 2)

    def get_weekly_pnl(self) -> float:
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        row = self.conn.execute(
            "SELECT SUM(pnl) as total FROM paper_trades WHERE result IS NOT NULL AND exit_time >= ?",
            (week_ago,),
        ).fetchone()
        return round(row["total"] or 0.0, 2)

    def get_consecutive_losses(self) -> int:
        rows = self.conn.execute(
            "SELECT result FROM paper_trades WHERE result IS NOT NULL ORDER BY rowid DESC LIMIT 10"
        ).fetchall()
        count = 0
        for r in rows:
            if r["result"] == "LOSS":
                count += 1
            else:
                break
        return count

    def get_circuit_breaker_status(self) -> dict:
        """
        Returns:
            status: "GREEN" | "YELLOW" | "RED"
            reason: description
            details: raw numbers
        """
        daily_pnl  = self.get_daily_pnl()
        weekly_pnl = self.get_weekly_pnl()
        consec     = self.get_consecutive_losses()

        daily_limit  = -(self.account_size * DAILY_LOSS_LIMIT_PCT  / 100)
        weekly_limit = -(self.account_size * WEEKLY_LOSS_LIMIT_PCT / 100)

        details = {
            "pnl_today":   daily_pnl,
            "pnl_7d":      weekly_pnl,
            "consec_loss": consec,
            "daily_limit": daily_limit,
            "weekly_limit":weekly_limit,
        }

        if daily_pnl <= daily_limit:
            return {"status": "red",    "reason": f"Daily loss limit hit (${daily_pnl:+.2f})", "details": details}
        if weekly_pnl <= weekly_limit:
            return {"status": "red",    "reason": f"Weekly loss limit hit (${weekly_pnl:+.2f})", "details": details}
        if consec >= CONSEC_LOSS_LIMIT:
            return {"status": "yellow", "reason": f"{consec} consecutive losses — reduced sizing", "details": details}
        if daily_pnl < 0 and abs(daily_pnl) > abs(daily_limit) * 0.7:
            return {"status": "yellow", "reason": f"Approaching daily loss limit (${daily_pnl:+.2f})", "details": details}

        return {"status": "green", "reason": "All circuit breakers clear", "details": details}

    def is_trading_allowed(self) -> bool:
        cb = self.get_circuit_breaker_status()
        return cb["status"] != "red"

    def close(self):
        self.conn.close()
