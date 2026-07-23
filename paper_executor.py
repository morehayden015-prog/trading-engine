"""
paper_executor.py — Paper trade executor
Dynamic risk scaling 0.25–3% based on score and recent performance.
Tracks open positions, session stats, and P&L in SQLite.
"""
import os
import uuid
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DB_PATH      = os.getenv("DB_PATH", "trades.db")
# Starting balance only — NOT the current account value. Use
# get_current_equity() everywhere risk sizing, margin, or circuit breakers
# need "how much money is actually in the account right now", so that
# realized P&L compounds into position sizing and loss-limit thresholds
# instead of every calculation silently pinning to day-one's $10k forever.
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "10000"))


def get_current_equity(db_path: str = None) -> float:
    """
    Current paper-account equity = starting balance + realized P&L from
    every CLOSED trade. This is the number that should back risk sizing,
    margin display, and circuit-breaker thresholds — previously all three
    used the static ACCOUNT_SIZE constant directly, so a winning trade
    never actually grew the account: risk-per-trade stayed pinned to the
    original $10k forever, and the dashboard's margin card never reflected
    accumulated profit either.
    """
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status='CLOSED'"
        ).fetchone()
        realized_pnl = row[0] or 0.0
    finally:
        conn.close()
    return round(ACCOUNT_SIZE + realized_pnl, 2)

# Trading timezone for day/week/month boundaries — handles EST/EDT automatically.
TRADING_TZ = ZoneInfo("America/New_York")

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
        # Compounding: risk dollars scale with current equity (starting
        # balance + realized P&L), not a fixed $10k forever.
        equity     = get_current_equity(DB_PATH)
        risk_usd   = round(equity * (risk_pct / 100), 2)
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

    def get_recent_or_open_trade(self, symbol: str, strategy: str, cooldown_minutes: int = 20) -> dict | None:
        """
        Returns the most recent paper_trades row for this symbol+strategy if
        it's still OPEN/PARTIAL, or was CLOSED within the last
        `cooldown_minutes` — otherwise None.

        Used to block a fresh entry into a structure we already just traded.
        This exists because the signal-level dedupe (main.py's
        _is_duplicate_signal, and scanner.py's own in-memory _fired_signals
        cooldown) both key on exact price within a short window, or live only
        in process memory. A re-scan minutes later at a moved price sails
        through both — and an in-memory cooldown is silently wiped by any
        process restart (deploy, crash) regardless of price. This check reads
        the durable DB directly so it survives both gaps.
        """
        row = self.conn.execute(
            "SELECT trade_id, status, exit_time FROM paper_trades "
            "WHERE symbol=? AND strategy=? ORDER BY entry_time DESC LIMIT 1",
            (symbol, strategy),
        ).fetchone()
        if not row:
            return None

        if row["status"] in ("OPEN", "PARTIAL"):
            return dict(row)

        if row["status"] == "CLOSED" and row["exit_time"]:
            exit_dt = self._parse_exit_time(row["exit_time"])
            if datetime.now(timezone.utc) - exit_dt < timedelta(minutes=cooldown_minutes):
                return dict(row)

        return None

    def get_open_trades(self) -> list:
        # PARTIAL trades still carry real exposure (the runner leg from a
        # TP1 scale-out hasn't hit TP2/TP3/SL yet) — they belong here, not
        # just plain 'OPEN' rows, or that exposure silently disappears from
        # every view that calls this (dashboard, /trades, position sizing).
        # be_moved/tp1_hit/current_sl are added by trade_monitor_agent's
        # _ensure_management_columns() the first time it runs, so a brand
        # new DB won't have them yet — probe for them instead of assuming.
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
        mgmt_select = ""
        if "be_moved" in cols:
            mgmt_select += ", COALESCE(be_moved, 0) as be_moved"
        if "current_sl" in cols:
            mgmt_select += ", current_sl"
        rows = self.conn.execute(
            "SELECT trade_id, symbol, direction, strategy, entry_price, entry_time, "
            f"risk_pct, score{mgmt_select} "
            "FROM paper_trades WHERE status IN ('OPEN', 'PARTIAL')"
        ).fetchall()
        trades = [dict(r) for r in rows]

        # Attach live SL/TP1/TP2/TP3 price levels so callers (dashboard, /trades)
        # don't need to duplicate trade_monitor_agent's distance table.
        try:
            from trade_monitor_agent import compute_trade_levels
            for t in trades:
                levels = compute_trade_levels(
                    symbol=t["symbol"],
                    direction=t["direction"],
                    entry_price=t["entry_price"],
                    current_sl=t.get("current_sl"),
                    be_moved=bool(t.get("be_moved", 0)),
                )
                t.update(levels)
        except Exception:
            log.warning("Could not compute TP/SL levels for open trades", exc_info=True)

        return trades

    def get_open_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE status IN ('OPEN', 'PARTIAL')"
        ).fetchone()
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
            "win_rate": win_rate, "total_pnl": total_pl,
        }

    def get_today_stats(self) -> dict:
        """
        Wins/losses/BE + P&L for trades closed today, using the same
        America/New_York day boundary as get_performance_summary() — this
        previously used a UTC calendar-date boundary instead, so the
        dashboard's "Today" card and the "TODAY'S WIN/LOSS" panel could
        disagree by several hours' worth of trades (UTC midnight is
        7-8pm NY, mid-session).
        """
        now_local   = datetime.now(timezone.utc).astimezone(TRADING_TZ)
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self.conn.execute(
            "SELECT result, pnl, exit_time FROM paper_trades WHERE status='CLOSED' AND exit_time IS NOT NULL"
        ).fetchall()
        rows = [r for r in rows if self._parse_exit_time(r["exit_time"]).astimezone(TRADING_TZ) >= today_start]
        total    = len(rows)
        wins     = sum(1 for r in rows if r["result"] == "WIN")
        losses   = sum(1 for r in rows if r["result"] == "LOSS")
        be       = sum(1 for r in rows if r["result"] == "BE")
        total_pl = round(sum(r["pnl"] or 0 for r in rows), 2)
        win_rate = round(wins / total, 3) if total > 0 else None
        return {
            "total": total, "wins": wins, "losses": losses, "be": be,
            "win_rate": win_rate, "total_pnl": total_pl,
        }

    @staticmethod
    def _parse_exit_time(ts: str) -> datetime:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def get_performance_summary(self) -> dict:
        """
        TODAY / THIS WEEK (Mon-Sun) / THIS MONTH / THIS YEAR performance,
        computed from closed trades, with day boundaries in America/New_York
        (EST/EDT) — the account's trading timezone.
        """
        rows = self.conn.execute(
            "SELECT result, pnl, exit_time FROM paper_trades WHERE status='CLOSED' AND exit_time IS NOT NULL"
        ).fetchall()

        now_local   = datetime.now(timezone.utc).astimezone(TRADING_TZ)
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start  = today_start - timedelta(days=today_start.weekday())  # Monday
        month_start = today_start.replace(day=1)
        year_start  = today_start.replace(month=1, day=1)

        def bucket(since: datetime) -> dict:
            matched = [r for r in rows if self._parse_exit_time(r["exit_time"]).astimezone(TRADING_TZ) >= since]
            wins    = sum(1 for r in matched if r["result"] == "WIN")
            losses  = sum(1 for r in matched if r["result"] == "LOSS")
            total   = len(matched)
            net_pnl = round(sum(r["pnl"] or 0 for r in matched), 2)
            return {
                "wins": wins, "losses": losses, "total": total,
                "net_pnl": net_pnl,
                "win_rate": round(wins / total, 3) if total > 0 else None,
                "label": "GREEN" if net_pnl >= 0 else "RED",
            }

        today = bucket(today_start)
        week  = bucket(week_start)
        month = bucket(month_start)
        year  = bucket(year_start)

        return {
            "today": {
                "wins": today["wins"], "losses": today["losses"],
                "net_pnl": today["net_pnl"], "win_rate": today["win_rate"],
            },
            "this_week": {
                "net_pnl": week["net_pnl"], "label": week["label"],
                "wins": week["wins"], "losses": week["losses"],
            },
            "this_month": {
                "net_pnl": month["net_pnl"], "label": month["label"],
                "wins": month["wins"], "losses": month["losses"],
            },
            "this_year": {
                "net_pnl": year["net_pnl"], "label": year["label"],
                "wins": year["wins"], "losses": year["losses"],
            },
            "timezone":    "America/New_York",
            "generated_at": now_local.isoformat(),
        }

    def get_margin_status(self) -> dict:
        """
        Margin committed to currently OPEN positions, analogous to a
        broker's 'margin used' readout on a paper trading account.
        Uses each open trade's risk_dollars (capital allocated) as its
        margin footprint since this is a fixed-R paper model rather than
        a live-priced margin account.
        """
        # A PARTIAL trade has already scaled half its size out at TP1, so
        # only half its original risk_dollars is still committed as margin
        # for the runner leg.
        row = self.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status='PARTIAL' THEN risk_dollars / 2.0 ELSE risk_dollars END), 0) as used, "
            "COUNT(*) as n "
            "FROM paper_trades WHERE status IN ('OPEN', 'PARTIAL')"
        ).fetchone()
        # Current equity (starting balance + realized P&L), not the static
        # starting-balance constant — so the dashboard's margin card actually
        # reflects accumulated profit/loss instead of always showing day one.
        equity    = get_current_equity(DB_PATH)
        used      = round(row["used"] or 0.0, 2)
        available = round(max(equity - used, 0.0), 2)
        used_pct  = round((used / equity) * 100, 2) if equity else 0.0
        return {
            "account_size":   equity,
            "margin_used":    used,
            "margin_available": available,
            "margin_used_pct": used_pct,
            "open_positions": row["n"] or 0,
        }

    def close(self):
        self.conn.close()
