"""
paper_executor.py — Paper trade executor
Dynamic risk scaling 0.25–3% based on score and recent performance.
Tracks open positions, session stats, and P&L in SQLite.
"""
import os
import uuid
import sqlite3
import logging
from datetime import datetime

log = logging.getLogger(__name__)

DB_PATH      = os.getenv("DB_PATH", "trades.db")
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "10000"))

# Risk config
BASE_RISK_PCT = float(os.getenv("BASE_RISK_PCT", "1.0"))   # 1% default
MIN_RISK_PCT  = 0.25
MAX_RISK_PCT  = 3.0

# RR defaults by symbol
DEFAULT_RR = {
    "XAUUSD": 2.0,
    "ES":     2.5,
    "NQ":     3.0,
    "CL":     2.0,
    # Forex majors — tighter scalp RR
    "EURUSD": 1.5,
    "GBPUSD": 1.5,
    "USDJPY": 1.5,
    "AUDUSD": 1.5,
}


class PaperExecutor:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            trade_id     TEXT PRIMARY KEY,
            symbol       TEXT,
            direction    TEXT,
            strategy     TEXT,
            entry_price  REAL,
            entry_time   TEXT,
            exit_price   REAL,
            exit_time    TEXT,
            result       TEXT,
            risk_pct     REAL,
            risk_dollars REAL,
            rr           REAL,
            pnl          REAL,
            score        REAL,
            ai_reasoning TEXT,
            status       TEXT DEFAULT 'OPEN'
        )
        """)
        self.conn.commit()

    def _calc_risk(self, score: float) -> float:
        """
        Dynamic risk scaling:
        score >= 8.5 → 1.5× base
        score >= 7.5 → 1.0× base
        score >= 6.5 → 0.75× base
        below 6.5 should not reach here (filtered upstream)
        """
        if score >= 8.5:
            mult = 1.5
        elif score >= 7.5:
            mult = 1.0
        else:
            mult = 0.75

        risk = BASE_RISK_PCT * mult
        return round(max(MIN_RISK_PCT, min(MAX_RISK_PCT, risk)), 2)

    def open_trade(
        self,
        symbol: str,
        direction: str,
        price: float,
        strategy: str,
        score: float,
        ai_reasoning: str = "",
        timeframe: str = "5m",
        sizing_multiplier: float = 1.0,
    ) -> dict:
        trade_id   = str(uuid.uuid4())[:8].upper()
        risk_pct   = round(self._calc_risk(score) * sizing_multiplier, 2)
        risk_pct   = max(0.25, min(3.0, risk_pct))
        risk_usd   = round(ACCOUNT_SIZE * (risk_pct / 100), 2)
        rr         = DEFAULT_RR.get(symbol, 2.0)
        entry_time = datetime.utcnow().isoformat()

        self.conn.execute(
            """INSERT INTO paper_trades
               (trade_id, symbol, direction, strategy, entry_price, entry_time,
                risk_pct, risk_dollars, rr, score, ai_reasoning, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, symbol, direction, strategy, price, entry_time,
             risk_pct, risk_usd, rr, score, ai_reasoning[:500], "OPEN"),
        )
        self.conn.commit()

        trade = {
            "trade_id":    trade_id,
            "symbol":      symbol,
            "direction":   direction,
            "strategy":    strategy,
            "entry_price": price,
            "entry_time":  entry_time,
            "risk_pct":    risk_pct,
            "risk_usd":    risk_usd,
            "rr":          rr,
            "score":       score,
        }
        log.info(f"Paper trade opened | {trade_id} | {symbol} {direction} @ {price} | risk={risk_pct}%")
        return trade

    def close_trade(self, trade_id: str, result: str, exit_price: float = None):
        row = self.conn.execute(
            "SELECT * FROM paper_trades WHERE trade_id=?", (trade_id,)
        ).fetchone()

        if not row:
            log.error(f"Trade not found: {trade_id}")
            return

        risk_usd = row["risk_dollars"]
        rr       = row["rr"]

        if result == "WIN":
            pnl = round(risk_usd * rr, 2)
        elif result == "LOSS":
            pnl = round(-risk_usd, 2)
        else:  # BE
            pnl = 0.0

        self.conn.execute(
            "UPDATE paper_trades SET result=?, exit_price=?, exit_time=?, pnl=?, status='CLOSED' WHERE trade_id=?",
            (result, exit_price, datetime.utcnow().isoformat(), pnl, trade_id),
        )
        self.conn.commit()
        log.info(f"Paper trade closed | {trade_id} | {result} | P&L={pnl:+.2f}")

    def get_open_trades(self) -> list:
        rows = self.conn.execute(
            "SELECT trade_id, symbol, direction, strategy, entry_price, entry_time, risk_pct, score FROM paper_trades WHERE status='OPEN'"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_open_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as n FROM paper_trades WHERE status='OPEN'").fetchone()
        return row["n"] if row else 0

    def get_session_stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT result, pnl FROM paper_trades WHERE status='CLOSED' ORDER BY rowid DESC LIMIT 50"
        ).fetchall()
        total    = len(rows)
        wins     = sum(1 for r in rows if r["result"] == "WIN")
        losses   = sum(1 for r in rows if r["result"] == "LOSS")
        total_pl = round(sum(r["pnl"] or 0 for r in rows), 2)
        win_rate = round(wins / total, 3) if total > 0 else None
        return {
            "total": total, "wins": wins, "losses": losses,