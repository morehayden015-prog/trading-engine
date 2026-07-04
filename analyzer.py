"""
analyzer.py — Trade Performance Analyzer
Level 1 Self-Learning System
Analyzes closed trades from paper_trades table and produces
a performance matrix per strategy/symbol combination.
"""

import sqlite3
import json
from datetime import datetime
from collections import defaultdict


DB_PATH = "trades.db"
MIN_TRADES_FOR_ANALYSIS = 5  # Minimum trades before adjusting a strategy


def get_closed_trades():
    """Pull all closed trades from paper_trades."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("""
        SELECT * FROM paper_trades
        WHERE status = 'CLOSED'
        ORDER BY exit_time ASC
    """)
    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return trades


def analyze_performance(trades):
    """
    Build a performance matrix grouped by strategy + symbol.
    Returns a dict with win rate, avg R, profit factor, trade count.
    """
    grouped = defaultdict(list)

    for trade in trades:
        strategy = trade.get("strategy", "unknown")
        symbol = trade.get("symbol", "unknown")
        key = f"{strategy}::{symbol}"
        grouped[key].append(trade)

    performance = {}

    for key, group in grouped.items():
        strategy, symbol = key.split("::")
        total = len(group)

        if total < MIN_TRADES_FOR_ANALYSIS:
            print(f"[ANALYZER] Skipping {key} — only {total} trades (need {MIN_TRADES_FOR_ANALYSIS})")
            continue

        wins = [t for t in group if t.get("result", "").upper() == "WIN"]
        losses = [t for t in group if t.get("result", "").upper() == "LOSS"]

        win_rate = len(wins) / total if total > 0 else 0

        rr_values = [t.get("rr", 0) for t in group if t.get("rr") is not None]
        avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0

        pnl_values = [t.get("pnl", 0) for t in group if t.get("pnl") is not None]
        gross_profit = sum(p for p in pnl_values if p > 0)
        gross_loss = abs(sum(p for p in pnl_values if p < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else gross_profit

        avg_score = sum(t.get("score", 0) for t in group) / total

        performance[key] = {
            "strategy": strategy,
            "symbol": symbol,
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 4),
            "avg_rr": round(avg_rr, 4),
            "profit_factor": round(profit_factor, 4),
            "avg_score": round(avg_score, 4),
            "total_pnl": round(sum(pnl_values), 4),
        }

    return performance


def determine_weight_adjustment(stats):
    """
    Determine weight multiplier based on performance stats.
    Returns (multiplier, reason) tuple.
    """
    win_rate = stats["win_rate"]
    avg_rr = stats["avg_rr"]
    profit_factor = stats["profit_factor"]

    # Top performer — scale up
    if win_rate >= 0.60 and avg_rr >= 1.5 and profit_factor >= 1.5:
        return 1.5, "Top performer: WR >= 60%, Avg R >= 1.5, PF >= 1.5"

    # Strong performer — mild scale up
    if win_rate >= 0.55 and avg_rr >= 1.2:
        return 1.25, "Strong performer: WR >= 55%, Avg R >= 1.2"

    # Acceptable — hold current weight
    if win_rate >= 0.50 and avg_rr >= 1.0:
        return 1.0, "Acceptable performance: holding weight"

    # Underperforming — scale down
    if win_rate >= 0.40:
        return 0.75, "Underperforming: WR 40-50%, scaling down"

    # Poor performer — disable
    return 0.0, f"Poor performer: WR below 40% ({round(win_rate*100, 1)}%), disabling"


def run_analysis():
    """
    Main analysis function.
    Returns performance matrix with weight recommendations.
    """
    print(f"\n[ANALYZER] Running trade analysis — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    trades = get_closed_trades()

    if not trades:
        print("[ANALYZER] No closed trades found. Nothing to analyze.")
        return {}

    print(f"[ANALYZER] Found {len(trades)} closed trades to analyze")

    performance = analyze_performance(trades)

    if not performance:
        print(f"[ANALYZER] No strategy has reached {MIN_TRADES_FOR_ANALYSIS}+ trades yet.")
        return {}

    # Add weight recommendations to each entry
    for key, stats in performance.items():
        multiplier, reason = determine_weight_adjustment(stats)
        stats["weight_multiplier"] = multiplier
        stats["weight_reason"] = reason

    print(f"[ANALYZER] Analysis complete — {len(performance)} strategy/symbol combos evaluated")
    return performance


if __name__ == "__main__":
    results = run_analysis()
    if results:
        print("\n--- PERFORMANCE MATRIX ---")
        for key, stats in results.items():
            print(f"\n{key}")
            print(f"  Trades: {stats['total_trades']} | WR: {stats['win_rate']*100:.1f}% | Avg R: {stats['avg_rr']} | PF: {stats['profit_factor']}")
            print(f"  Total PnL: {stats['total_pnl']} | Recommended multiplier: {stats['weight_multiplier']}x")
            print(f"  Reason: {stats['weight_reason']}")
