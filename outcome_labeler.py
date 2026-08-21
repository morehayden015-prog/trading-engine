"""
outcome_labeler.py — Manual trade outcome labeling for the /outcome endpoint.

The price-based auto-labeling that used to live here (check_price_based) was
dead code — nothing ever called it — and carried its own copy of the TP/SL
table that was missing every forex pair, so a EURUSD trade would have been
given the XAUUSD row's 4.0-price-unit stop. Automatic closing is owned by
trade_monitor_agent.py; levels live in levels.py.
"""
import os
import sqlite3
import logging
from datetime import datetime

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "trades.db")


class OutcomeLabeler:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def label_manual(self, trade_id: str, result: str, exit_price: float = None, notes: str = "") -> bool:
        """Manually label a trade WIN/LOSS/BE."""
        result = result.upper()
        if result not in ("WIN", "LOSS", "BE"):
            log.error(f"Invalid result: {result}")
            return False

        c = self.conn.cursor()
        c.execute(
            "UPDATE paper_trades SET result=?, exit_price=?, status='CLOSED' WHERE trade_id=? AND status='OPEN'",
            (result, exit_price, trade_id),
        )
        # Also update signals table
        c.execute(
            "UPDATE signals SET result=? WHERE trade_id=?",
            (result, trade_id),
        )
        self.conn.commit()

        rows_affected = c.rowcount
        if rows_affected > 0:
            log.info(f"Manually labeled: {trade_id} → {result}")
            return True
        else:
            log.warning(f"Trade {trade_id} not found or already closed")
            return False

    def get_unlabeled(self, limit: int = 20) -> list:
        rows = self.conn.execute(
            "SELECT trade_id, symbol, direction, entry_price, entry_time FROM paper_trades WHERE result IS NULL AND status='OPEN' ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
