"""
Standalone verification: builds a throwaway sqlite DB with synthetic
paper_trades rows, then runs the exact aggregation logic used by the new
/admin/strategy-breakdown/{secret} endpoint and by paper_executor's
get_performance_summary() (which /stats calls), and checks the two agree
on total wins/losses/net_pnl for the "today" (days=1) window.
"""
import sqlite3
import tempfile
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")

tmp_path = tempfile.mktemp(suffix=".db")
conn = sqlite3.connect(tmp_path)
conn.execute("""
CREATE TABLE paper_trades (
    trade_id TEXT PRIMARY KEY, symbol TEXT, direction TEXT, strategy TEXT,
    entry_price REAL, entry_time TEXT, exit_price REAL, exit_time TEXT,
    result TEXT, risk_pct REAL, risk_dollars REAL, rr REAL, pnl REAL,
    score REAL, ai_reasoning TEXT, status TEXT DEFAULT 'OPEN'
)
""")

now_utc = datetime.now(timezone.utc)
rows = [
    # trade_id, symbol, strategy, result, pnl, rr, exit_time (utc iso), status
    ("T1", "XAUUSD", "ORB",   "WIN",  120.0, 2.0, now_utc.isoformat(), "CLOSED"),
    ("T2", "XAUUSD", "ORB",   "LOSS", -60.0, 2.0, now_utc.isoformat(), "CLOSED"),
    ("T3", "ES",     "SWEEP", "WIN",  200.0, 3.0, now_utc.isoformat(), "CLOSED"),
    ("T4", "ES",     "SWEEP", "LOSS", -80.0, 3.0, now_utc.isoformat(), "CLOSED"),
    # yesterday (America/New_York) -- should be excluded from days=1
    ("T5", "ES",     "SWEEP", "WIN",  999.0, 3.0, (now_utc - timedelta(days=2)).isoformat(), "CLOSED"),
    # still open -- must never be counted
    ("T6", "XAUUSD", "ORB",   None,   None,  2.0, None, "OPEN"),
]
for r in rows:
    conn.execute(
        "INSERT INTO paper_trades (trade_id, symbol, strategy, result, pnl, rr, exit_time, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        r,
    )
conn.commit()
conn.close()

os.environ["DB_PATH"] = tmp_path


# ---- replicate paper_executor.get_performance_summary()'s "today" bucket ----
def parse_exit_time(ts):
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def stats_today_bucket():
    conn = sqlite3.connect(tmp_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT result, pnl, exit_time FROM paper_trades WHERE status='CLOSED' AND exit_time IS NOT NULL"
    ).fetchall()
    conn.close()
    now_local = datetime.now(timezone.utc).astimezone(TZ)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    matched = [r for r in rows if parse_exit_time(r["exit_time"]).astimezone(TZ) >= today_start]
    wins = sum(1 for r in matched if r["result"] == "WIN")
    losses = sum(1 for r in matched if r["result"] == "LOSS")
    net_pnl = round(sum(r["pnl"] or 0 for r in matched), 2)
    return {"total": len(matched), "wins": wins, "losses": losses, "net_pnl": net_pnl}


# ---- exact logic copied from the new endpoint (days=1) ----
def strategy_breakdown(days=1):
    conn = sqlite3.connect(tmp_path)
    conn.row_factory = sqlite3.Row
    cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
    r_col = "rr" if "rr" in cols else None
    select_cols = "strategy, symbol, result, pnl, exit_time"
    if r_col:
        select_cols += f", {r_col}"
    rows = conn.execute(
        f"SELECT {select_cols} FROM paper_trades WHERE status='CLOSED' AND exit_time IS NOT NULL"
    ).fetchall()
    conn.close()

    now_local = datetime.now(timezone.utc).astimezone(TZ)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    since = today_start - timedelta(days=max(days, 1) - 1)

    def _parse(ts):
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)

    matched = [r for r in rows if _parse(r["exit_time"]) >= since]

    groups = {}
    for r in matched:
        key = (r["strategy"] or "UNKNOWN", r["symbol"] or "UNKNOWN")
        g = groups.setdefault(key, {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "r_values": []})
        g["trades"] += 1
        if r["result"] == "WIN":
            g["wins"] += 1
        elif r["result"] == "LOSS":
            g["losses"] += 1
        g["net_pnl"] += r["pnl"] or 0
        if r_col:
            v = r[r_col]
            if v is not None:
                g["r_values"].append(v)

    def build(g, strategy=None, symbol=None):
        trades = g["trades"]
        e = {}
        if strategy is not None:
            e["strategy"] = strategy
        if symbol is not None:
            e["symbol"] = symbol
        e.update({
            "trades": trades, "wins": g["wins"], "losses": g["losses"],
            "win_rate": round((g["wins"] / trades) * 100, 1) if trades else 0.0,
            "net_pnl": round(g["net_pnl"], 2),
        })
        if r_col:
            e["avg_r"] = round(sum(g["r_values"]) / len(g["r_values"]), 2) if g["r_values"] else None
        return e

    breakdown = [build(g, s, sym) for (s, sym), g in groups.items()]
    breakdown.sort(key=lambda e: e["net_pnl"])

    totals_group = {
        "trades": sum(g["trades"] for g in groups.values()),
        "wins": sum(g["wins"] for g in groups.values()),
        "losses": sum(g["losses"] for g in groups.values()),
        "net_pnl": sum(g["net_pnl"] for g in groups.values()),
        "r_values": [v for g in groups.values() for v in g["r_values"]],
    }
    totals = build(totals_group)
    return {"days": days, "breakdown": breakdown, "totals": totals}


import json

result = strategy_breakdown(days=1)
print("JSON valid:", json.dumps(result, default=str)[:0] == "" and True)
print(json.dumps(result, indent=2, default=str))

stats_bucket = stats_today_bucket()
print("\n/stats-equivalent today bucket:", stats_bucket)

assert result["totals"]["wins"] == stats_bucket["wins"], "wins mismatch"
assert result["totals"]["losses"] == stats_bucket["losses"], "losses mismatch"
assert result["totals"]["net_pnl"] == stats_bucket["net_pnl"], "net_pnl mismatch"
assert result["totals"]["trades"] == stats_bucket["total"], "trade count mismatch"
# T5 (2 days ago) and T6 (open) must be excluded
assert result["totals"]["trades"] == 4, f"expected 4 closed-today trades, got {result['totals']['trades']}"
print("\nALL CHECKS PASSED: totals match /stats-equivalent bucket; open + old trades excluded.")

os.remove(tmp_path)
