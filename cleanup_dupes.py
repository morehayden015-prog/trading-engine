"""
cleanup_dupes.py — One-time cleanup of duplicate trades in trades.db

Finds duplicate trades (same symbol + strategy + direction + entry_price, with
entry_time within 10s of each other), keeps the earliest trade in each group,
and deletes the rest — but only after backing up the DB and getting your
explicit "yes" confirmation.

Usage:
    python cleanup_dupes.py                        # uses DB_PATH env var or trades.db
    python cleanup_dupes.py --db /app/data/trades.db   # e.g. against a mounted volume path
"""
import os
import sys
import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Clean up duplicate trades in trades.db")
    parser.add_argument("--db", default=None, help="Path to trades.db (defaults to DB_PATH env var or trades.db)")
    args = parser.parse_args()

    db_path = args.db or os.getenv("DB_PATH", "trades.db")

    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        sys.exit(1)

    # Set DB_PATH before importing dedupe_lib/strategy_manager so every module
    # that reads the env var at import time points at the same file.
    os.environ["DB_PATH"] = db_path

    from dedupe_lib import (
        backup_db, find_duplicate_groups, delete_duplicates,
        refresh_strategy_stats, build_preview, DUPE_WINDOW_SECONDS,
    )

    print(f"\n{'=' * 70}")
    print(f"  Duplicate Trade Cleanup — {db_path}")
    print(f"  (dedupe window: {DUPE_WINDOW_SECONDS}s)")
    print(f"{'=' * 70}\n")

    groups = find_duplicate_groups(db_path=db_path, window_seconds=DUPE_WINDOW_SECONDS)
    if not groups:
        print("No duplicate trades found. Nothing to do.\n")
        return

    preview = build_preview(groups)

    print(f"Found {len(groups)} duplicate group(s) — {len(preview)} trade(s) slated for deletion:\n")
    for p in preview:
        pnl_str = f"${p['pnl']:+.2f}" if p["pnl"] is not None else "N/A (open)"
        print(
            f"  DELETE  {p['trade_id']}  {p['symbol']:<7} {p['direction']:<6} "
            f"@ {p['entry_price']:<10}  P&L={pnl_str:<14} entry={p['entry_time']}  "
            f"(dup of kept trade {p['kept_trade_id']})"
        )
    print()

    # Backup BEFORE any deletion, regardless of whether the user confirms.
    backup_path = backup_db(db_path)
    print(f"Backup saved to: {backup_path}\n")

    confirm = input(f"Type 'yes' to permanently delete these {len(preview)} trade(s): ").strip().lower()
    if confirm != "yes":
        print("\nAborted — no trades were deleted. The backup above was still created.")
        return

    trade_ids = [p["trade_id"] for p in preview]
    total_pnl_removed = delete_duplicates(trade_ids, db_path=db_path)

    print(f"\nDeleted {len(trade_ids)} duplicate trade(s).")
    print(f"Total P&L removed: ${total_pnl_removed:+.2f}")
    print("(Your paper balance/win-rate stats were distorted by this amount before cleanup.)\n")

    print("Refreshing strategy stats...")
    try:
        summary = refresh_strategy_stats()
        print(f"Strategy auto-management refreshed | disabled={summary['disabled']} enabled={summary['enabled']}\n")
    except Exception as e:
        print(f"Could not refresh strategy stats: {e}\n")

    print(f"{'=' * 70}")
    print("  Cleanup complete.")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
