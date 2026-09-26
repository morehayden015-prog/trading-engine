"""
learning_logger.py — Self-Learning Audit Logger
Level 1 Self-Learning System

Writes a human-readable narrative of every learning cycle to
logs/learning_log.txt — not just *what* changed, but *how* the bot
got there: what data it pulled, how it grouped it, and the reasoning
behind each decision. Also logs the separate calibrate.py heuristic
cycle (scoring-component weights), which previously had no persistent
record at all.

Each session can also carry an `ai_reasoning` string — the bot's own,
Claude-generated, plain-English reflection on why it did what it did
this cycle (see ai_brain.explain_learning_cycle). That's written into
both the text log and the JSONL record so it can be surfaced on the
dashboard and in Discord updates.
"""

import os
import json
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_FILE = LOG_DIR / "learning_log.txt"
LOG_FILE_JSONL = LOG_DIR / "learning_log.jsonl"


def ensure_log_dir():
    LOG_DIR.mkdir(exist_ok=True)


def _append_jsonl(record: dict):
    with open(LOG_FILE_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _write_reasoning_block(f, ai_reasoning: str = None):
    if not ai_reasoning:
        return
    f.write("\nBot's own reasoning:\n")
    f.write(f"  {ai_reasoning}\n")


def log_adjustment_session(changes, data_summary: dict = None, performance: dict = None, ai_reasoning: str = None):
    """
    Log a strategy-weight self-learning session (analyzer.py + weight_adjuster.py).
    data_summary: output of analyzer.get_data_collection_summary() — describes
                  what data was pulled and how (the "collection process").
    performance:  full performance matrix per strategy/symbol, even for groups
                  that didn't change weight — shows the full reasoning, not
                  just the outcome.
    ai_reasoning: the bot's own plain-English explanation of this cycle,
                  from ai_brain.explain_learning_cycle (optional).
    """
    ensure_log_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n" + "=" * 60 + "\n")
        f.write(f"SELF-LEARNING SESSION — {timestamp}\n")
        f.write("=" * 60 + "\n")

        if data_summary:
            f.write("How this session collected its data:\n")
            f.write(f"  Source            : {data_summary.get('source')}\n")
            f.write(f"  Trades pulled     : {data_summary.get('n_trades')}\n")
            dr = data_summary.get("date_range") or {}
            f.write(f"  Date range        : {dr.get('earliest', 'n/a')} → {dr.get('latest', 'n/a')}\n")
            f.write(f"  Min trades/group  : {data_summary.get('min_trades_per_group')} "
                    f"(groups below this are skipped — not enough signal yet)\n\n")

        if performance:
            f.write(f"Groups evaluated ({len(performance)} strategy/symbol combos with enough trades):\n")
            for key, stats in performance.items():
                f.write(f"  {key}\n")
                f.write(f"    Trades: {stats['total_trades']} | Win rate: {stats['win_rate']*100:.1f}% | "
                        f"Avg R: {stats['avg_rr']} | Profit factor: {stats['profit_factor']}\n")
                f.write(f"    Decision: {stats['weight_reason']} → multiplier x{stats['weight_multiplier']}\n")
            f.write("\n")

        if not changes:
            f.write("Outcome: No weight changes made this session — nothing crossed a decision threshold.\n")
            _write_reasoning_block(f, ai_reasoning)
            _append_jsonl({
                "timestamp": timestamp, "type": "adjustment", "changes": [],
                "data_summary": data_summary, "performance": performance,
                "ai_reasoning": ai_reasoning,
            })
            return

        f.write(f"Outcome: {len(changes)} weight change(s) applied:\n\n")
        for change in changes:
            f.write(f"Strategy : {change['strategy']}\n")
            f.write(f"Symbol   : {change['symbol']}\n")
            f.write(f"Weight   : {change['previous_weight']} → {change['new_weight']} (x{change['multiplier_applied']})\n")
            f.write(f"Win Rate : {round(change['win_rate']*100, 1)}%\n")
            f.write(f"Avg R    : {change['avg_rr']}\n")
            f.write(f"Trades   : {change['total_trades']}\n")
            f.write(f"Reason   : {change['reason']}\n")
            f.write(f"Time     : {change['timestamp']}\n")
            f.write("-" * 40 + "\n")

        _write_reasoning_block(f, ai_reasoning)

    _append_jsonl({
        "timestamp": timestamp, "type": "adjustment", "changes": changes,
        "data_summary": data_summary, "performance": performance,
        "ai_reasoning": ai_reasoning,
    })
    print(f"[LOGGER] Learning log updated — {LOG_FILE}")


def log_no_data_session(data_summary: dict = None, ai_reasoning: str = None):
    ensure_log_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n" + "=" * 60 + "\n")
        f.write(f"SELF-LEARNING SESSION — {timestamp}\n")
        f.write("=" * 60 + "\n")
        if data_summary:
            f.write(f"Pulled {data_summary.get('n_trades', 0)} closed trades from "
                    f"{data_summary.get('source')} — not enough to analyze any group "
                    f"(need {data_summary.get('min_trades_per_group')}+ per strategy/symbol).\n")
        else:
            f.write("Insufficient trade data for analysis. No changes made.\n")
        _write_reasoning_block(f, ai_reasoning)
    _append_jsonl({
        "timestamp": timestamp, "type": "no_data", "data_summary": data_summary,
        "ai_reasoning": ai_reasoning,
    })
    print(f"[LOGGER] No-data session logged — {LOG_FILE}")


def log_calibration_session(result: dict):
    """
    Log a calibrate.py session — the separate heuristic system that tunes
    scoring-component weights (session/strategy/timeframe/ai) rather than
    per-strategy multipliers. This previously had no persistent narrative.

    result may carry an "ai_reasoning" key (the bot's own explanation of
    this cycle, attached by auto_calibrate.py before calling this) — logged
    the same way as the self-learning sessions above.
    """
    ensure_log_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ai_reasoning = result.get("ai_reasoning")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n" + "=" * 60 + "\n")
        f.write(f"CALIBRATION SESSION — {timestamp}\n")
        f.write("=" * 60 + "\n")

        if result.get("skipped"):
            f.write(f"Skipped: {result.get('reason')}\n")
            _write_reasoning_block(f, ai_reasoning)
            _append_jsonl({"timestamp": timestamp, "type": "calibration", "result": result})
            return

        f.write("How this session collected its data:\n")
        f.write("  Source        : paper_trades table (last 100 labelled trades, most recent first)\n")
        f.write(f"  Trades pulled : {result.get('n_outcomes')}\n\n")

        f.write(f"Win rate            : {result.get('win_rate', 0)*100:.1f}%\n")
        f.write(f"Avg score (wins)    : {result.get('avg_score_wins')}\n")
        f.write(f"Avg score (losses)  : {result.get('avg_score_losses')}\n")
        f.write(f"Score gap           : {result.get('score_gap')}\n\n")

        changes = result.get("changes", {})
        if not changes:
            f.write("Decision: Performance in normal range — scoring weights held steady.\n")
        else:
            f.write("Decision: Adjusted scoring-component weights —\n")
            for comp, ch in changes.items():
                f.write(f"  {comp}: {ch['old']} → {ch['new']} ({ch['diff']:+})\n")
            f.write(f"\nResulting weights: {json.dumps(result.get('new_weights', {}))}\n")

        _write_reasoning_block(f, ai_reasoning)

    _append_jsonl({"timestamp": timestamp, "type": "calibration", "result": result})
    print(f"[LOGGER] Calibration session logged — {LOG_FILE}")


def read_learning_log():
    if not LOG_FILE.exists():
        print("[LOGGER] No learning log found yet.")
        return
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        print(f.read())


def read_recent_sessions(limit: int = 10) -> list:
    """Structured recent sessions — used by dashboard.py."""
    if not LOG_FILE_JSONL.exists():
        return []
    with open(LOG_FILE_JSONL, "r", encoding="utf-8") as f:
        lines = f.readlines()
    records = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(records))


if __name__ == "__main__":
    read_learning_log()
