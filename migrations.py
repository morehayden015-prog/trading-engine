"""
migrations.py — backup + baseline snapshot + idempotent startup migrations.

Runs once at startup (called from main.py before anything else opens the DB)
and is safe to run on every startup: if nothing is pending it does nothing
except record a "startup" baseline row.

Rules this module enforces:
  * Additive only. Migrations may CREATE TABLE IF NOT EXISTS and
    ALTER TABLE ... ADD COLUMN (via add_column_if_missing). Nothing here
    drops, truncates, deletes or recreates anything.
  * Before ANY pending migration is applied, the DB is copied to
    <db>.bak-YYYYMMDD-HHMM in the same directory (the Railway volume).
  * A baseline snapshot (trades, wins, losses, net P&L, per-strategy counts,
    plus a hash of every trade's outcome fields) is taken before and after.
    If any historical number changed, the backup is restored and startup
    is aborted.

CLI:
    python migrations.py status     # applied migrations + current snapshot
    python migrations.py snapshot   # print the current baseline snapshot
    python migrations.py verify     # re-check every stored baseline against today's data
    python migrations.py migrate    # run pending migrations (same as startup)
"""
import os
import sys
import json
import shutil
import sqlite3
import hashlib
import logging
from datetime import datetime

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "trades.db")


class MigrationError(RuntimeError):
    pass


# ── helpers migrations are allowed to use ────────────────────────────────────

def table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def column_exists(conn, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def add_column_if_missing(conn, table: str, column: str, decl: str) -> bool:
    """ALTER TABLE ... ADD COLUMN, guarded by PRAGMA table_info. Returns True if added."""
    if not table_exists(conn, table) or column_exists(conn, table, column):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


# ── migrations ───────────────────────────────────────────────────────────────
# Append only. Never edit or reorder an entry that has shipped.

def _m001_baseline_snapshots(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS baseline_snapshots (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            taken_at  TEXT NOT NULL,
            label     TEXT NOT NULL,
            snapshot  TEXT NOT NULL
        )
    """)


MIGRATIONS = [
    ("001_baseline_snapshots", _m001_baseline_snapshots),
]


# ── baseline snapshot ────────────────────────────────────────────────────────

def _utcnow() -> str:
    # Same format the executor writes exit_time in (naive UTC isoformat),
    # so the two compare correctly as strings.
    return datetime.utcnow().isoformat()


def take_snapshot(conn, as_of: str = None) -> dict:
    """
    Baseline of trade history.

    as_of=None  → everything in the table right now.
    as_of=<ts>  → only trades already CLOSED at that moment (exit_time <= ts).
                  Closed trades never change, so this is what lets an old
                  baseline be re-checked later even though new trades have
                  been added since.
    """
    snap = {
        "taken_at": _utcnow(), "as_of": as_of,
        "total_trades": 0, "open_trades": 0, "closed_trades": 0,
        "wins": 0, "losses": 0, "other_results": 0,
        "net_pnl": 0.0, "per_strategy": {}, "history_hash": None,
    }
    if not table_exists(conn, "paper_trades"):
        return snap

    where, params = "", ()
    if as_of:
        where = "WHERE status='CLOSED' AND exit_time IS NOT NULL AND exit_time <= ?"
        params = (as_of,)

    rows = conn.execute(
        f"SELECT trade_id, strategy, status, result, pnl, exit_time "
        f"FROM paper_trades {where} ORDER BY trade_id", params
    ).fetchall()

    h = hashlib.sha256()
    closed_h = hashlib.sha256()
    pnl_cents = 0
    for trade_id, strategy, status, result, pnl, exit_time in rows:
        snap["total_trades"] += 1
        key = strategy or "(none)"
        per = snap["per_strategy"].setdefault(key, {"trades": 0, "closed": 0})
        per["trades"] += 1
        line = f"{trade_id}|{strategy}|{status}|{result}|{pnl!r}|{exit_time}\n".encode()
        h.update(line)
        if status == "CLOSED":
            snap["closed_trades"] += 1
            per["closed"] += 1
            closed_h.update(line)
            if result == "WIN":
                snap["wins"] += 1
            elif result == "LOSS":
                snap["losses"] += 1
            else:
                snap["other_results"] += 1
            pnl_cents += round((pnl or 0) * 100)
        else:
            snap["open_trades"] += 1
    snap["net_pnl"] = round(pnl_cents / 100, 2)
    snap["history_hash"] = h.hexdigest()
    snap["closed_hash"] = closed_h.hexdigest()
    return snap


# Fields that must be identical before vs. after a migration.
_STRICT_FIELDS = ("total_trades", "open_trades", "closed_trades", "wins", "losses",
                  "other_results", "net_pnl", "per_strategy", "history_hash")
# Fields compared when re-checking an old baseline against closed history.
_CLOSED_FIELDS = ("closed_trades", "wins", "losses", "other_results", "net_pnl", "closed_hash")


def diff_snapshots(before: dict, after: dict, fields=_STRICT_FIELDS) -> list:
    return [
        {"field": f, "before": before.get(f), "after": after.get(f)}
        for f in fields if before.get(f) != after.get(f)
    ]


def record_snapshot(conn, label: str, snap: dict):
    if table_exists(conn, "baseline_snapshots"):
        conn.execute(
            "INSERT INTO baseline_snapshots (taken_at, label, snapshot) VALUES (?,?,?)",
            (snap["taken_at"], label, json.dumps(snap, sort_keys=True)),
        )
        conn.commit()


def verify_stored_baselines(db_path: str = None) -> list:
    """
    Re-check every stored baseline: recompute the numbers for trades that were
    already closed when the baseline was taken and compare. Any mismatch means
    old history was altered after the fact.
    """
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    out = []
    try:
        if not table_exists(conn, "baseline_snapshots"):
            return out
        stored = conn.execute(
            "SELECT id, taken_at, label, snapshot FROM baseline_snapshots ORDER BY id"
        ).fetchall()
        for sid, taken_at, label, raw in stored:
            old = json.loads(raw)
            if "closed_hash" not in old:
                continue
            now = take_snapshot(conn, as_of=taken_at)
            diffs = diff_snapshots(old, now, _CLOSED_FIELDS)
            out.append({"id": sid, "taken_at": taken_at, "label": label,
                        "closed_trades": old.get("closed_trades"),
                        "net_pnl": old.get("net_pnl"),
                        "ok": not diffs, "diffs": diffs})
    finally:
        conn.close()
    return out


# ── backup ───────────────────────────────────────────────────────────────────

def backup_before_migration(db_path: str) -> str:
    """Consistent copy of the DB to <db>.bak-YYYYMMDD-HHMM next to it. Returns the path."""
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M")
    dest = f"{db_path}.bak-{ts}"
    if os.path.exists(dest):
        dest = f"{db_path}.bak-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)   # SQLite online-backup API: safe even with WAL / open writers
    finally:
        dst.close()
        src.close()
    check = sqlite3.connect(dest)
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if ok != "ok":
        raise MigrationError(f"Backup {dest} failed integrity_check: {ok}")
    return dest


def _restore(backup_path: str, db_path: str):
    """Put the pre-migration backup back. The failed DB is kept aside, never deleted."""
    failed = f"{db_path}.failed-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    os.replace(db_path, failed)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.replace(db_path + suffix, failed + suffix)
    shutil.copy2(backup_path, db_path)
    log.error(f"Restored {db_path} from {backup_path}; failed copy kept at {failed}")


# ── runner ───────────────────────────────────────────────────────────────────

def _applied(conn) -> set:
    if not table_exists(conn, "schema_migrations"):
        return set()
    return {r[0] for r in conn.execute("SELECT id FROM schema_migrations")}


def _flag_path(db_path: str) -> str:
    return db_path + ".migration-failed"


def run_migrations(db_path: str = None, migrations: list = None) -> dict:
    """
    Apply pending migrations. No-op (apart from one 'startup' baseline row)
    when everything is already applied.
    """
    db_path = db_path or DB_PATH
    migrations = MIGRATIONS if migrations is None else migrations
    report = {"db_path": db_path, "applied_now": [], "already_applied": [],
              "backup": None, "before": None, "after": None, "status": "ok"}

    flag = _flag_path(db_path)
    if os.path.exists(flag) and os.getenv("MIGRATION_RETRY") != "1":
        raise MigrationError(
            f"A previous migration failed and the backup was restored (see {flag}). "
            "Refusing to retry automatically. Fix the migration, then set "
            "MIGRATION_RETRY=1 for one deploy to try again."
        )

    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    is_new = not os.path.exists(db_path) or os.path.getsize(db_path) == 0

    conn = sqlite3.connect(db_path)
    try:
        done = _applied(conn)
        pending = [(mid, fn) for mid, fn in migrations if mid not in done]
        report["already_applied"] = sorted(done)

        if not pending:
            snap = take_snapshot(conn)
            report["before"] = report["after"] = snap
            record_snapshot(conn, "startup", snap)
            return report

        before = take_snapshot(conn)
        report["before"] = before
    finally:
        conn.close()

    if not is_new:
        report["backup"] = backup_before_migration(db_path)
        log.info(f"Pre-migration backup written: {report['backup']}")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id         TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)
        conn.commit()
        for mid, fn in pending:
            try:
                fn(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (id, applied_at) VALUES (?,?)",
                    (mid, _utcnow()),
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise MigrationError(f"Migration {mid} raised: {e}") from e
            report["applied_now"].append(mid)
            log.info(f"Migration applied: {mid}")

        after = take_snapshot(conn)
        report["after"] = after
        diffs = diff_snapshots(before, after)
        if diffs:
            raise MigrationError(f"Historical numbers changed during migration: {diffs}")

        record_snapshot(conn, "pre-migration " + ",".join(report["applied_now"]), before)
        record_snapshot(conn, "post-migration " + ",".join(report["applied_now"]), after)
    except MigrationError as e:
        conn.close()
        report["status"] = "failed"
        if report["backup"]:
            _restore(report["backup"], db_path)
            with open(flag, "w") as f:
                json.dump({"at": _utcnow(), "error": str(e), "backup": report["backup"]}, f)
        log.error(f"MIGRATION FAILED — startup aborted: {e}")
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if os.path.exists(flag):
        os.replace(flag, flag + "-resolved-" + datetime.utcnow().strftime("%Y%m%d-%H%M%S"))
    return report


def status(db_path: str = None) -> dict:
    """Read-only summary used by /health and /admin/db-status."""
    db_path = db_path or DB_PATH
    abs_path = os.path.abspath(db_path)
    db_dir = os.path.dirname(abs_path)
    out = {
        "db_path": db_path,
        "db_exists": os.path.exists(db_path),
        "db_size_bytes": os.path.getsize(db_path) if os.path.exists(db_path) else 0,
        "db_dir": db_dir,
        "db_dir_is_mount": os.path.ismount(db_dir),
        "migrations_known": [m[0] for m in MIGRATIONS],
        "migrations_applied": [],
        "migration_failed_flag": os.path.exists(_flag_path(db_path)),
    }
    if out["db_exists"]:
        conn = sqlite3.connect(db_path)
        try:
            if table_exists(conn, "schema_migrations"):
                out["migrations_applied"] = [
                    {"id": r[0], "applied_at": r[1]}
                    for r in conn.execute("SELECT id, applied_at FROM schema_migrations ORDER BY id")
                ]
            out["snapshot"] = take_snapshot(conn)
        finally:
            conn.close()
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "migrate":
        print(json.dumps(run_migrations(), indent=2, sort_keys=True))
    elif cmd == "snapshot":
        c = sqlite3.connect(DB_PATH)
        print(json.dumps(take_snapshot(c), indent=2, sort_keys=True))
        c.close()
    elif cmd == "verify":
        res = verify_stored_baselines()
        print(json.dumps(res, indent=2))
        sys.exit(0 if all(r["ok"] for r in res) else 1)
    else:
        print(json.dumps(status(), indent=2, sort_keys=True))
