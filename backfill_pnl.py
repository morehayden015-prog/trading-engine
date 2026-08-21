"""
backfill_pnl.py — one-off historical P&L correction.

Recomputes `pnl` for every CLOSED trade from the price it actually exited at,
replacing the synthetic planned-RR figure the old close logic wrote (see
paper_executor._realized_pnl for the full explanation).

Non-destructive: the original value is copied to a new `pnl_synthetic` column
before anything is overwritten, and that copy is only ever taken once, so
re-running can't lose the pre-correction numbers.

Dry run by default — prints what would change and writes nothing:

    python backfill_pnl.py --db /app/data/trades.db

Apply for real (takes a timestamped file copy of the DB first):

    python backfill_pnl.py --db /app/data/trades.db --apply

Run it with the service stopped, or at least outside market hours: it rewrites
rows the running process also reads for equity and risk sizing.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime


def _load_realized_pnl():
    """Reuse the live P&L function so this script can't drift from it."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from paper_executor import _realized_pnl
    return _realized_pnl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("DB_PATH", "trades.db"))
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"No such database: {args.db}")

    realized_pnl = _load_realized_pnl()

    if args.apply:
        stamp  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup = os.path.join(os.path.dirname(args.db) or ".", f"trades_prebackfill_{stamp}.db")
        shutil.copy2(args.db, backup)
        print(f"Backed up {args.db} -> {backup}\n")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_trades)")}
    if "pnl_synthetic" not in cols:
        if args.apply:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN pnl_synthetic REAL")
            conn.execute("UPDATE paper_trades SET pnl_synthetic = pnl")
            conn.commit()
            print("Added pnl_synthetic column and preserved the original values.\n")
        else:
            print("Would add pnl_synthetic column and preserve the original values.\n")

    rows = list(conn.execute("SELECT * FROM paper_trades WHERE status='CLOSED'"))

    changed = unmapped = 0
    old_total = new_total = 0.0
    updates = []

    for r in rows:
        old = r["pnl"] or 0.0
        new = realized_pnl(r, r["result"], r["exit_price"])
        old_total += old
        new_total += new
        if r["exit_price"] is None:
            unmapped += 1
        if abs(new - old) > 0.005:
            changed += 1
            updates.append((new, r["trade_id"]))

    print(f"closed trades        : {len(rows)}")
    print(f"rows that change     : {changed}")
    print(f"rows with no exit px : {unmapped}  (left on the planned-RR fallback)")
    print(f"stored net P&L       : {old_total:+.2f}")
    print(f"corrected net P&L    : {new_total:+.2f}")
    print(f"difference           : {new_total - old_total:+.2f}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to commit.")
        conn.close()
        return

    conn.executemany("UPDATE paper_trades SET pnl=? WHERE trade_id=?", updates)
    conn.commit()
    conn.close()
    print(f"\nApplied. {len(updates)} rows updated; originals preserved in pnl_synthetic.")
    print("Account equity is derived from SUM(pnl), so risk sizing on the next")
    print("trade will reflect the corrected balance immediately.")


if __name__ == "__main__":
    main()
