"""
db_backup.py — daily copy of the trades DB to a second location.

Every day the loop writes <BACKUP_DIR>/trades-YYYYMMDD.db using SQLite's
online-backup API (safe while the bot is writing), integrity-checks the copy,
and keeps the newest BACKUP_KEEP daily files. It only ever prunes files it
created itself (trades-YYYYMMDD.db inside BACKUP_DIR) — never the live DB and
never the pre-migration *.bak-* files.

Env:
    BACKUP_DIR    where daily copies go (default: <DB dir>/backups)
    BACKUP_KEEP   how many daily copies to keep (default 30; 0 = keep all)

Restore steps: see docs/BACKUP_RESTORE.md
"""
import os
import re
import shutil
import asyncio
import sqlite3
import logging
from datetime import datetime

log = logging.getLogger(__name__)

DB_PATH     = os.getenv("DB_PATH", "trades.db")
BACKUP_DIR  = os.getenv("BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "30"))

_DAILY_RE = re.compile(r"^trades-\d{8}\.db$")


def make_backup(db_path: str = None, backup_dir: str = None, day: str = None) -> str | None:
    """Write today's backup if it doesn't exist yet. Returns the path (None if no DB)."""
    db_path    = db_path or DB_PATH
    backup_dir = backup_dir or BACKUP_DIR
    if not os.path.exists(db_path):
        return None
    os.makedirs(backup_dir, exist_ok=True)
    day  = day or datetime.utcnow().strftime("%Y%m%d")
    dest = os.path.join(backup_dir, f"trades-{day}.db")
    if os.path.exists(dest):
        return dest

    tmp = dest + ".partial"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    check = sqlite3.connect(tmp)
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if ok != "ok":
        log.error(f"Daily backup failed integrity_check ({ok}); left at {tmp}")
        return None
    os.replace(tmp, dest)
    log.info(f"Daily DB backup written: {dest} ({os.path.getsize(dest)} bytes)")
    return dest


def list_backups(db_path: str = None, backup_dir: str = None) -> list:
    """Daily backups plus the pre-migration / dedupe backups that sit next to the DB."""
    db_path    = db_path or DB_PATH
    backup_dir = backup_dir or BACKUP_DIR
    db_dir     = os.path.dirname(os.path.abspath(db_path))
    db_name    = os.path.basename(db_path)
    found = []
    if os.path.isdir(backup_dir):
        for name in os.listdir(backup_dir):
            if _DAILY_RE.match(name):
                found.append(("daily", os.path.join(backup_dir, name)))
    if os.path.isdir(db_dir):
        for name in os.listdir(db_dir):
            if name.startswith(db_name + ".bak-"):
                found.append(("pre-migration", os.path.join(db_dir, name)))
            elif name.startswith(db_name + ".replaced-") and not name.endswith(("-wal", "-shm")):
                found.append(("replaced-by-restore", os.path.join(db_dir, name)))
            elif name.startswith(db_name + ".failed-") and not name.endswith(("-wal", "-shm")):
                found.append(("failed-migration", os.path.join(db_dir, name)))
            elif name.startswith("trades_backup_") and name.endswith(".db"):
                found.append(("pre-dedupe", os.path.join(db_dir, name)))
    return sorted(
        ({"kind": k, "name": os.path.basename(p), "bytes": os.path.getsize(p),
          "modified_utc": datetime.utcfromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds")}
         for k, p in found),
        key=lambda b: b["modified_utc"], reverse=True,
    )


def resolve_backup(name: str, db_path: str = None, backup_dir: str = None) -> str | None:
    """Map a bare filename from list_backups() back to its full path (no path traversal)."""
    db_path    = db_path or DB_PATH
    backup_dir = backup_dir or BACKUP_DIR
    name = os.path.basename(name)
    db_dir  = os.path.dirname(os.path.abspath(db_path))
    db_name = os.path.basename(db_path)
    if _DAILY_RE.match(name):
        path = os.path.join(backup_dir, name)
    elif (name.startswith((db_name + ".bak-", db_name + ".replaced-", db_name + ".failed-"))
          and not name.endswith(("-wal", "-shm"))) or (name.startswith("trades_backup_") and name.endswith(".db")):
        path = os.path.join(db_dir, name)
    else:
        return None
    return path if os.path.isfile(path) else None


def prune(backup_dir: str = None, keep: int = None) -> list:
    backup_dir = backup_dir or BACKUP_DIR
    keep = BACKUP_KEEP if keep is None else keep
    if keep <= 0 or not os.path.isdir(backup_dir):
        return []
    daily = sorted(n for n in os.listdir(backup_dir) if _DAILY_RE.match(n))
    removed = []
    for name in daily[:-keep]:
        os.remove(os.path.join(backup_dir, name))
        removed.append(name)
    if removed:
        log.info(f"Pruned old daily backups: {removed}")
    return removed


async def daily_backup_loop():
    """Checks every 10 minutes; writes one backup per UTC day (and one right at startup)."""
    while True:
        try:
            if make_backup():
                prune()
        except Exception as e:
            log.error(f"Daily backup error: {e}")
        await asyncio.sleep(600)


def restore_if_requested(db_path: str = None, backup_dir: str = None) -> str | None:
    """
    One-shot restore, driven by the RESTORE_FROM env var (a backup filename
    from list_backups()). Runs at startup before anything opens the DB.

    The current DB is moved aside to <db>.replaced-<timestamp> (never deleted),
    then the backup is copied into place. A marker file makes sure the same
    RESTORE_FROM value is only ever applied once, so leaving the variable set
    cannot roll the DB back again on the next restart.
    """
    name = os.getenv("RESTORE_FROM", "").strip()
    if not name:
        return None
    db_path = db_path or DB_PATH
    marker = f"{db_path}.restored-{os.path.basename(name)}"
    if os.path.exists(marker):
        log.warning(f"RESTORE_FROM={name} was already applied; ignoring. Remove the variable.")
        return None
    src = resolve_backup(name, db_path, backup_dir)
    if not src:
        raise RuntimeError(f"RESTORE_FROM={name}: no such backup. Startup aborted; nothing was changed.")
    check = sqlite3.connect(src)
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if ok != "ok":
        raise RuntimeError(f"RESTORE_FROM={name}: backup failed integrity_check ({ok}). Nothing was changed.")

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    if os.path.exists(db_path):
        os.replace(db_path, f"{db_path}.replaced-{ts}")
        for suffix in ("-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                os.replace(db_path + suffix, f"{db_path}.replaced-{ts}{suffix}")
    shutil.copy2(src, db_path)
    with open(marker, "w") as f:
        f.write(f"restored from {src} at {ts} UTC\n")
    log.warning(f"DB RESTORED from {src}. Previous DB kept at {db_path}.replaced-{ts}")
    return src
