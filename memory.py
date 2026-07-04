"""
memory.py — SQLite-backed trade memory
Stores signals, outcomes, and provides rolling context for the AI brain.
"""
import os
import json
import sqlite3
import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "trades.db")


class Memory:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        c = self.conn.cursor()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id    TEXT,
            symbol      TEXT,
            direction   TEXT,
            strategy    TEXT,
            score       REAL,
            result      TEXT DEFAULT NULL,
            regime      TEXT DEFAULT NULL,
            timestamp   TEXT
        );

        CREATE TABLE IF NOT EXISTS context_cache (
            symbol      TEXT PRIMARY KEY,
            data        TEXT,
            updated_at  TEXT
        );
        """)
        self.conn.commit()

    def record_signal(self, symbol: str, direction: str, strategy: str, score: float, trade_id: str = None, regime: str = None):
        c = self.conn.cursor()
        c.execute(
            "INSERT INTO signals (trade_id, symbol, direction, strategy, score, regime, timestamp) VALUES (?,?,?,?,?,?,?)",
            (trade_id, symbol, direction, strategy, score, regime, datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def record_outcome(self, trade_id: str, result: str):
        c = self.conn.cursor()
        c.execute("UPDATE signals SET result=? WHERE trade_id=?", (result.upper(), trade_id))
        self.conn.commit()
        log.info(f"Outcome recorded: trade_id={trade_id} result={result}")

    def get_context(self, symbol: str) -> dict:
        c = self.conn.cursor()

        # Last 5 signals for this symbol
        rows = c.execute(
            "SELECT direction, strategy, score, result, timestamp FROM signals WHERE symbol=? ORDER BY id DESC LIMIT 5",
            (symbol,),
        ).fetchall()

        recent_signals = [
            {"direction": r["direction"], "strategy": r["strategy"], "score": round(r["score"], 2), "result": r["result"]}
            for r in rows
        ]

        # Win rate over last 20 labelled trades
        labelled = c.execute(
            "SELECT result FROM signals WHERE symbol=? AND result IS NOT NULL ORDER BY id DESC LIMIT 20",
            (symbol,),
        ).fetchall()

        wins = sum(1 for r in labelled if r["result"] == "WIN")
        win_rate = round(wins / len(labelled), 2) if labelled else None

        # Consecutive losses
        consecutive_losses = 0
        all_labelled = c.execute(
            "SELECT result FROM signals WHERE symbol=? AND result IS NOT NULL ORDER BY id DESC LIMIT 10",
            (symbol,),
        ).fetchall()
        for r in all_labelled:
            if r["result"] == "LOSS":
                consecutive_losses += 1
            else:
                break

        # Last regime
        last_regime_row = c.execute(
            "SELECT regime FROM signals WHERE symbol=? AND regime IS NOT NULL ORDER BY id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        last_regime = last_regime_row["regime"] if last_regime_row else "UNKNOWN"

        return {
            "recent_signals":     recent_signals,
            "win_rate_20":        win_rate,
            "consecutive_losses": consecutive_losses,
            "last_regime":        last_regime,
        }

    def get_all_outcomes(self, symbol: str = None, limit: int = 100) -> list:
        c = self.conn.cursor()
        if symbol:
            rows = c.execute(
                "SELECT * FROM signals WHERE symbol=? AND result IS NOT NULL ORDER BY id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM signals WHERE result IS NOT NULL ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_strategy_stats(self, symbol: str, strategy: str, n: int = 20) -> dict:
        c = self.conn.cursor()
        rows = c.execute(
            "SELECT result, score FROM signals WHERE symbol=? AND strategy=? AND result IS NOT NULL ORDER BY id DESC LIMIT ?",
            (symbol, strategy, n),
        ).fetchall()

        if not rows:
            return {"win_rate": None, "avg_score": None, "n": 0}

        wins    = sum(1 for r in rows if r["result"] == "WIN")
        scores  = [r["score"] for r in rows]
        return {
            "win_rate":  round(wins / len(rows), 3),
            "avg_score": round(sum(scores) / len(scores), 2),
            "n":         len(rows),
        }

    def close(self):
        self.conn.close()
