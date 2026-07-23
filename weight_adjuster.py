"""
weight_adjuster.py — Strategy Weight Adjuster
Level 1 Self-Learning System
Takes performance matrix from analyzer.py and applies
weight adjustments to strategy_weights.json.
Creates the weights file if it doesn't exist.
"""

import json
import os
from datetime import datetime
from analyzer import run_analysis


WEIGHTS_FILE = "strategy_weights.json"
MAX_WEIGHT = 2.0   # Cap — no strategy can scale beyond 2x
MIN_WEIGHT = 0.0   # Floor — 0 means disabled


def load_weights():
    """Load existing strategy weights or create defaults."""
    if os.path.exists(WEIGHTS_FILE):
        with open(WEIGHTS_FILE, "r") as f:
            return json.load(f)

    # Default weights — all strategies start at 1.0
    print(f"[ADJUSTER] No weights file found — creating defaults")
    defaults = {
        "sweep_bos_fvg::XAUUSD": 1.0,
        "rp_profits::ES": 1.0,
        "rp_profits::NQ": 1.0,
        "rp_profits::XAUUSD": 1.0,
        "ict_5step::NQ": 1.0,
        "ict_5step::ES": 1.0,
        "ict_5step::XAUUSD": 1.0,
        "orb_scalp::ES": 1.0,
        "orb_scalp::NQ": 1.0,
        "orb_scalp::CL": 1.0,
        "supply_demand::XAUUSD": 1.0,
        "supply_demand::ES": 1.0,
        "supply_demand::NQ": 1.0,
        "supply_demand::CL": 1.0,
        "mamba_scalp::NQ": 1.0,
        "mamba_scalp::ES": 1.0,
    }
    save_weights(defaults)
    return defaults


def save_weights(weights):
    """Save updated weights to file."""
    with open(WEIGHTS_FILE, "w") as f:
        json.dump(weights, f, indent=2)


def apply_adjustments(current_weights, performance_matrix):
    """
    Apply weight adjustments based on performance matrix.
    Returns (updated_weights, list of changes made).
    """
    updated = current_weights.copy()
    changes = []

    for key, stats in performance_matrix.items():
        multiplier = stats["weight_multiplier"]
        reason = stats["weight_reason"]
        strategy = stats["strategy"]
        symbol = stats["symbol"]

        current = current_weights.get(key, 1.0)

        if multiplier == 0.0:
            # Disable the strategy
            new_weight = 0.0
        else:
            # Apply multiplier to current weight, cap at MAX_WEIGHT
            new_weight = min(current * multiplier, MAX_WEIGHT)
            # Floor at MIN_WEIGHT
            new_weight = max(new_weight, MIN_WEIGHT)

        new_weight = round(new_weight, 4)

        if new_weight != current:
            changes.append({
                "key": key,
                "strategy": strategy,
                "symbol": symbol,
                "previous_weight": current,
                "new_weight": new_weight,
                "multiplier_applied": multiplier,
                "reason": reason,
                "win_rate": stats["win_rate"],
                "avg_rr": stats["avg_rr"],
                "total_trades": stats["total_trades"],
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            updated[key] = new_weight
            print(f"[ADJUSTER] {key}: {current} → {new_weight} ({reason})")
        else:
            print(f"[ADJUSTER] {key}: No change (weight stays at {current})")

    return updated, changes


def run_adjustment():
    """
    Main adjustment function.
    Runs analysis, applies weight changes, saves results.
    """
    print(f"\n[ADJUSTER] Starting weight adjustment — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    performance = run_analysis()

    if not performance:
        print("[ADJUSTER] No performance data available. Skipping adjustment.")
        return

    current_weights = load_weights()
    updated_weights, changes = apply_adjustments(current_weights, performance)
    save_weights(updated_weights)
    print(f"\n[ADJUSTER] Weights saved to {WEIGHTS_FILE}")
    return changes


if __name__ == "__main__":
    changes = run_adjustment()
    if changes:
        print(f"\n[ADJUSTER] {len(changes)} weight(s) updated")
    else:
        print("\n[ADJUSTER] No weight changes needed")
