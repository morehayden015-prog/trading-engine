"""
dedupe_lib.py — Shared duplicate-trade detection & cleanup logic.

Used by:
  - cleanup_dupes.py           (one-time interactive CLI cleanup, local or via `railway run`)
  - main.py /admin/cleanup-dupes routes  (protected HTTP cleanup against the live Railway DB)

A "duplicate" is a trade with the same symbol + strategy + direction + entry_price
as another trade, whose entry_time falls within DUPE_WINDOW_SECONDS of it. Within
each duplicate group the earliest trade is the one to KEEP; the rest are deleted.
"""
import os
import shutil
import sqlite3
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "trades.db")
DUPE_WINDOW_SECONDS = 10


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def backup_db(db_path: str = None) -> str:
    """Copy the trades DB to trades_backup_[timestamp].db (same directory) and return the path."""
    db_path = db_path or DB_PATH
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.dirname(db_path) or "."
    backup_path = os.path.join(backup_dir, f"trades_backup_{ts}.db")
    shutil.copy2(db_path, backup_path)
    log.info(f"Backed up {db_path} -> {backup_path}")
    return backup_path


def find_duplicate_groups(db_path: str = None, window_seconds: int = DUPE_WINDOW_SECONDS) -> list:
    """
    Scan paper_trades for duplicate signals: same symbol + strategy + direction +
    entry_price, with entry_time within `window_seconds` of the previous trade in
    the same bucket (chained clustering — so 3 near-simultaneous dupes group together).

    Returns a list of groups. Each group is a list of row dicts sorted by entry_time,
    earliest first (index 0 = the trade to KEEP; the rest are duplicates to delete).
    """
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT trade_id, symbol, strategy, direction, entry_price, entry_time,
                  exit_time, result, pnl, status
           FROM paper_trades
           ORDER BY symbol, strategy, direction, entry_price, entry_time"""
    ).fetchall()
    conn.close()

    rows = [dict(r) for r in rows]
    groups = []
    bucket_key = object()
    bucket = []

    def flush_bucket():
        if len(bucket) < 2:
            return
        cluster = [bucket[0]]
        for r in bucket[1:]:
            prev_t = _parse_ts(cluster[-1]["entry_time"])
            cur_t = _parse_ts(r["entry_time"])
            if (cur_t - prev_t).total_seconds() <= window_seconds:
                cluster.append(r)
            else:
                if len(cluster) > 1:
                    groups.append(cluster)
                cluster = [r]
        if len(cluster) > 1:
            groups.append(cluster)

    for r in rows:
        key = (r["symbol"], r["strategy"], r["direction"], r["entry_price"])
        if key != bucket_key:
            flush_bucket()
            bucket_key = key
            bucket = [r]
        else:
            bucket.append(r)
    flush_bucket()

    return groups


def build_preview(groups: list) -> list:
    """Flatten duplicate groups into a preview list of trades slated for deletion (keeper excluded)."""
    preview = []
    for group in groups:
        keeper = group[0]
        for dupe in group[1:]:
            preview.append({
                "trade_id":      dupe["trade_id"],
                "symbol":        dupe["symbol"],
                "strategy":      dupe["strategy"],
                "direction":     dupe["direction"],
                "entry_price":   dupe["entry_price"],
                "entry_time":    dupe["entry_time"],
                "pnl":           dupe["pnl"],
                "status":        dupe["status"],
                "kept_trade_id": keeper["trade_id"],
            })
    return preview


def delete_duplicates(trade_ids: list, db_path: str = None) -> float:
    """Delete the given trade_ids. Returns the total P&L removed (rounded)."""
    db_path = db_path or DB_PATH
    if not trade_ids:
        return 0.0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(trade_ids))
    row = conn.execute(
        f"SELECT COALESCE(SUM(pnl), 0) as total_pnl FROM paper_trades WHERE trade_id IN ({placeholders})",
        trade_ids,
    ).fetchone()
    total_pnl_removed = round(row["total_pnl"] or 0.0, 2)
    conn.execute(f"DELETE FROM paper_trades WHERE trade_id IN ({placeholders})", trade_ids)
    conn.commit()
    conn.close()
    log.info(f"Deleted {len(trade_ids)} duplicate trade(s) | P&L removed: {total_pnl_removed:+.2f}")
    return total_pnl_removed


def refresh_strategy_stats() -> dict:
    """
    Re-run strategy auto-management (enable/disable based on win rate) so it
    reflects the cleaned data. strategy_manager.py computes stats live from
    paper_trades on every call, so this just forces that recompute + re-evaluation.
    """
    from strategy_manager import StrategyManager
    sm = StrategyManager()
    summary = sm.run_auto_management()
    sm.close()
    return summary
