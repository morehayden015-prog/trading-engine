"""
learning_logger.py — Self-Learning Audit Logger
Level 1 Self-Learning System
"""

import os
from datetime import datetime

LOG_FILE = "logs/learning_log.txt"


def ensure_log_dir():
    os.makedirs("logs", exist_ok=True)


def log_adjustment_session(changes):
    ensure_log_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n" + "="*60 + "\n")
        f.write(f"SELF-LEARNING SESSION — {timestamp}\n")
        f.write("="*60 + "\n")
        if not changes:
            f.write("No weight changes made this session.\n")
            return
        f.write(f"Changes made: {len(changes)}\n\n")
        for change in changes:
            f.write(f"Strategy : {change['strategy']}\n")
            f.write(f"Symbol   : {change['symbol']}\n")
            f.write(f"Weight   : {change['previous_weight']} → {change['new_weight']} (x{change['multiplier_applied']})\n")
            f.write(f"Win Rate : {round(change['win_rate']*100, 1)}%\n")
            f.write(f"Avg R    : {change['avg_rr']}\n")
            f.write(f"Trades   : {change['total_trades']}\n")
            f.write(f"Reason   : {change['reason']}\n")
            f.write(f"Time     : {change['timestamp']}\n")
            f.write("-"*40 + "\n")
    print(f"[LOGGER] Learning log updated — {LOG_FILE}")


def log_no_data_session():
    ensure_log_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n" + "="*60 + "\n")
        f.write(f"SELF-LEARNING SESSION — {timestamp}\n")
        f.write("="*60 + "\n")
        f.write("Insufficient trade data for analysis. No changes made.\n")
    print(f"[LOGGER] No-data session logged — {LOG_FILE}")


def read_learning_log():
    if not os.path.exists(LOG_FILE):
        print("[LOGGER] No learning log found yet.")
        return
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        print(f.read())


if __name__ == "__main__":
    read_learning_log()