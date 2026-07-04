"""
outcome_labeler.py — Trade outcome labeling
Supports manual labeling via /outcome endpoint and
automatic labeling via price-based TP/SL detection.
"""
import os
import sqlite3
import logging
from datetime import datetime

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "trades.db")

# Default TP/SL distances by symbol (in price units)
TP_DISTANCES = {
    "XAUUSD": {"TP1": 5.0, "TP2": 10.0, "TP3": 15.0, "SL": 4.0},
    "ES":     {"TP1": 5.0, "TP2": 10.0, "TP3": 20.0, "SL": 6.0},
    "NQ":     {"TP1": 15.0,"TP2": 30.0, "TP3": 60.0, "SL": 20.0},
    "CL":     {"TP1": 0.30,"TP2": 0.60, "TP3": 1.00, "SL": 0.25},
}


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

    def check_price_based(self, symbol: str, current_price: float) -> list:
        """
        Check all open trades for this symbol and auto-label
        if current price has hit TP1 or SL.
        Returns list of auto-labeled trade_ids.
        """
        c = self.conn.cursor()
        open_trades = c.execute(
            "SELECT trade_id, direction, entry_price FROM paper_trades WHERE symbol=? AND status='OPEN'",
            (symbol,),
        ).fetchall()

        levels = TP_DISTANCES.get(symbol, TP_DISTANCES["XAUUSD"])
        labeled = []

        for trade in open_trades:
            trade_id    = trade["trade_id"]
            direction   = trade["direction"]
            entry_price = trade["entry_price"]

            if direction == "LONG":
                tp1 = entry_price + levels["TP1"]
                sl  = entry_price - levels["SL"]
                if current_price >= tp1:
                    self.label_manual(trade_id, "WIN", current_price)
                    labeled.append({"trade_id": trade_id, "result": "WIN"})
                elif current_price <= sl:
                    self.label_manual(trade_id, "LOSS", current_price)
                    labeled.append({"trade_id": trade_id, "result": "LOSS"})
            else:  # SHORT
                tp1 = entry_price - levels["TP1"]
                sl  = entry_price + levels["SL"]
                if current_price <= tp1:
                    self.label_manual(trade_id, "WIN", current_price)
                    labeled.append({"trade_id": trade_id, "result": "WIN"})
                elif current_price >= sl:
                    self.label_manual(trade_id, "LOSS", current_price)
                    labeled.append({"trade_id": trade_id, "result": "LOSS"})

        return labeled

    def get_unlabeled(self, limit: int = 20) -> list:
        rows = self.conn.execute(
            "SELECT trade_id, symbol, direction, entry_price, entry_time FROM paper_trades WHERE result IS NULL AND status='OPEN' ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
