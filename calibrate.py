"""
calibrate.py — Weight calibration engine
Analyzes outcome history and adjusts scoring weights to improve future performance.
"""
import os
import json
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "trades.db")
LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(exist_ok=True)

CAL_LOG = LOG_DIR / "calibration_history.jsonl"

# Default weights (sum to 1.0)
DEFAULT_WEIGHTS = {
    "session":   0.20,
    "strategy":  0.25,
    "timeframe": 0.15,
    "ai":        0.40,
}

WEIGHT_BOUNDS = {
    "session":   (0.10, 0.35),
    "strategy":  (0.15, 0.40),
    "timeframe": (0.05, 0.25),
    "ai":        (0.25, 0.55),
}

WEIGHTS_FILE = "scoring_weights.json"
MIN_TRADES_TO_CALIBRATE = 10


def load_weights() -> dict:
    if os.path.exists(WEIGHTS_FILE):
        with open(WEIGHTS_FILE) as f:
            return json.load(f)
    return DEFAULT_WEIGHTS.copy()


def save_weights(weights: dict):
    with open(WEIGHTS_FILE, "w") as f:
        json.dump(weights, f, indent=2)
    log.info(f"Weights saved: {weights}")


def calibrate(force: bool = False) -> dict:
    """
    Run calibration. Analyzes recent outcomes and adjusts weights.
    Returns a dict with n_outcomes, changes, and new_weights.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT pt.score, pt.result, pt.symbol, pt.strategy
           FROM paper_trades pt
           WHERE pt.result IS NOT NULL
           ORDER BY pt.rowid DESC LIMIT 100"""
    ).fetchall()
    conn.close()

    n = len(rows)
    if n < MIN_TRADES_TO_CALIBRATE and not force:
        log.info(f"Calibration skipped: only {n} labelled trades (need {MIN_TRADES_TO_CALIBRATE})")
        return {"skipped": True, "reason": f"Need {MIN_TRADES_TO_CALIBRATE} trades, have {n}"}

    wins  = [r for r in rows if r["result"] == "WIN"]
    losses = [r for r in rows if r["result"] == "LOSS"]

    win_rate = len(wins) / n if n > 0 else 0.5

    current_weights = load_weights()
    new_weights     = current_weights.copy()
    changes         = {}

    # Simple heuristic: if AI confidence is strongly predictive, increase its weight
    if wins:
        avg_score_wins   = sum(r["score"] for r in wins) / len(wins)
    else:
        avg_score_wins = 7.0

    if losses:
        avg_score_losses = sum(r["score"] for r in losses) / len(losses)
    else:
        avg_score_losses = 6.0

    score_gap = avg_score_wins - avg_score_losses

    if win_rate > 0.60 and score_gap > 0.5:
        # Bot is performing well — small AI weight increase
        delta = 0.02
    elif win_rate < 0.45:
        # Poor performance — shift weight toward more deterministic components
        delta = -0.02
    else:
        delta = 0.0

    if delta != 0.0:
        old_ai = current_weights["ai"]
        new_ai = round(max(WEIGHT_BOUNDS["ai"][0], min(WEIGHT_BOUNDS["ai"][1], old_ai + delta)), 3)
        if new_ai != old_ai:
            new_weights["ai"] = new_ai
            # Redistribute difference
            diff = round(old_ai - new_ai, 3)
            new_weights["strategy"] = round(new_weights["strategy"] + diff / 2, 3)
            new_weights["session"]  = round(new_weights["session"]  + diff / 2, 3)
            changes["ai"]       = {"old": old_ai, "new": new_ai, "diff": round(delta, 3)}
            changes["strategy"] = {"old": current_weights["strategy"], "new": new_weights["strategy"], "diff": round(diff / 2, 3)}

    # Normalize
    total = sum(new_weights.values())
    new_weights = {k: round(v / total, 3) for k, v in new_weights.items()}

    if changes:
        save_weights(new_weights)

    entry = {
        "timestamp":   datetime.utcnow().isoformat(),
        "n_outcomes":  n,
        "win_rate":    round(win_rate, 3),
        "avg_score_wins":   round(avg_score_wins, 2),
        "avg_score_losses": round(avg_score_losses, 2),
        "score_gap":   round(score_gap, 2),
        "changes":     changes,
        "new_weights": new_weights,
    }

    with open(CAL_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

    log.info(f"Calibration complete | n={n} | WR={win_rate:.1%} | changes={len(changes)}")
    return entry
