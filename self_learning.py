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

    weights_file = "strategy_weights.json"

    if not os.path.exists(weights_file):
        return 1.0  # Default weight if file doesn't exist yet

    with open(weights_file, "r") as f:
        weights = json.load(f)

    key = f"{strategy}::{symbol}"
    return weights.get(key, 1.0)


def is_strategy_enabled(strategy, symbol):
    """
    Quick check — returns False if a strategy has been disabled (weight = 0).
    Use this in scanner.py or trade_scoring.py before processing signals.
    """
    weight = get_current_weight(strategy, symbol)
    return weight > 0.0


if __name__ == "__main__":
    run_self_learning_cycle(trigger="manual")
