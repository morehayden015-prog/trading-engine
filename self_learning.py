"""
self_learning.py — Self-Learning Orchestrator
Level 1 Self-Learning System
Main entry point. Runs the full self-learning cycle:
1. Analyze closed trades
2. Adjust strategy weights
3. Log all changes

Called by auto_calibrate.py on:
- Every Sunday (weekly cycle)
- Every 10 completed trades (trade-count trigger)

Can also be run manually: python self_learning.py
"""

from datetime import datetime
from analyzer import run_analysis
from weight_adjuster import run_adjustment
from learning_logger import log_adjustment_session, log_no_data_session


def run_self_learning_cycle(trigger="manual"):
    """
    Full self-learning cycle.
    trigger: 'manual', 'weekly', or 'trade_count'
    """
    print(f"\n{'='*60}")
    print(f"SELF-LEARNING CYCLE STARTED")
    print(f"Trigger : {trigger}")
    print(f"Time    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # Run weight adjustment (which internally runs analysis)
    changes = run_adjustment()

    # Log the session
    if changes is None:
        log_no_data_session()
    elif len(changes) == 0:
        log_adjustment_session([])
        print("\n[SELF-LEARNING] All weights stable — no adjustments needed")
    else:
        log_adjustment_session(changes)
        print(f"\n[SELF-LEARNING] Cycle complete — {len(changes)} weight(s) updated")

    print(f"{'='*60}\n")
    return changes


def get_current_weight(strategy, symbol):
    """
    Helper function for other modules to query current strategy weight.
    Returns float weight (0.0 = disabled, 1.0 = normal, 1.5 = boosted, etc.)
    """
    import json
    import os

    # Same file weight_adjuster writes — imported rather than re-declared so
    # the two can't drift apart. It previously hardcoded a bare relative path
    # here, which meant this lookup and the writer could resolve to different
    # files depending on the process CWD.
    from weight_adjuster import WEIGHTS_FILE as weights_file

    if not os.path.exists(weights_file):
        return 1.0  # Default weight if file doesn't exist yet

    with open(weights_file, "r") as f:
        weights = json.load(f)

    key = f"{strategy}::{symbol}"
    return weights.get(key, 1.0)


def is_strategy_enabled(strategy, symbol):
    """
    Quick check — returns False if a strategy has been disabled.
    Use this in scanner.py or trade_scoring.py before processing signals.

    The strategy_status table is authoritative, and the weights file is only
    consulted when that table has no opinion. It used to be the reverse (file
    only), which had two failure modes:

      1. The weights file lived on the container's ephemeral filesystem, so a
         redeploy wiped it and every disabled strategy silently resumed
         trading while strategy_status still reported it disabled.
      2. strategy_manager.run_auto_management() only writes the file on a
         state *transition*. For a pair already marked disabled in the DB,
         `currently` is already False, so the disable branch never re-fires
         and the re-enable branch needs a win rate the pair doesn't have —
         nothing could ever repair the file. The disable was unrecoverable
         precisely because it had already been recorded.

    Reading the DB first makes a disable survive both.
    """
    import os
    import sqlite3

    try:
        db_path = os.getenv("DB_PATH", "trades.db")
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT enabled FROM strategy_status WHERE key=?",
                    (f"{strategy}::{symbol}",),
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                return bool(row[0])
    except sqlite3.Error:
        # Table may not exist yet on a fresh DB — fall through to the file.
        pass

    return get_current_weight(strategy, symbol) > 0.0


if __name__ == "__main__":
    run_self_learning_cycle(trigger="manual")
