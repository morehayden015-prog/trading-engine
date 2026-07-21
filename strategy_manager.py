"""
strategy_manager.py — Strategy lifecycle manager
Tracks per-strategy performance (win rate, Sharpe ratio),
auto-enables/disables strategies based on recent results,
and provides rankings for the daily briefing.
"""
import os
import math
import sqlite3
import logging
from datetime import datetime, timezone

from weight_adjuster import load_weights as _load_strategy_weights, save_weights as _save_strategy_weights

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "trades.db")

# Minimum trades before auto-management acts on a strategy
MIN_TRADES = int(os.getenv("STRATEGY_MIN_TRADES", "10"))

# Win rate thresholds
DISABLE_BELOW  = float(os.getenv("STRATEGY_DISABLE_WR",  "0.35"))  # disable if WR < 35%
REENABLE_ABOVE = float(os.getenv("STRATEGY_REENABLE_WR", "0.50"))  # re-enable if WR recovers to 50%

ALL_STRATEGIES = [
    "sweep_bos_fvg", "rp_profits", "ict_5step", "orb_scalp",
    "supply_demand", "mamba_scalp",
]

# Every symbol the bot trades — previously futures-only, so forex
# strategy/symbol combos were never evaluated by auto-management at all.
ALL_MARKETS = ["XAUUSD", "ES", "NQ", "CL", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]


class StrategyManager:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_status (
            key         TEXT PRIMARY KEY,
            strategy    TEXT,
            symbol      TEXT,
            enabled     INTEGER DEFAULT 1,
            disabled_at TEXT,
            reason      TEXT
        )
        """)
        self.conn.commit()

    def _get_stats(self, strategy: str, symbol: str, n: int = 30) -> dict:
        """Pull recent closed trades for strategy/symbol and compute stats."""
        rows = self.conn.execute(
            """SELECT result, pnl, rr FROM paper_trades
               WHERE strategy=? AND symbol=? AND result IS NOT NULL
               ORDER BY rowid DESC LIMIT ?""",
            (strategy, symbol, n),
        ).fetchall()

        if not rows:
            return {"n": 0, "win_rate": None, "avg_pnl": None, "sharpe": None}

        total  = len(rows)
        wins   = sum(1 for r in rows if r["result"] == "WIN")
        pnls   = [r["pnl"] or 0.0 for r in rows]
        avg    = sum(pnls) / total
        std    = math.sqrt(sum((p - avg) ** 2 for p in pnls) / total) if total > 1 else 0.0
        sharpe = round(avg / std, 3) if std > 0 else 0.0

        return {
            "n":        total,
            "win_rate": round(wins / total, 3),
            "avg_pnl":  round(avg, 2),
            "sharpe":   sharpe,
        }

    def is_enabled(self, strategy: str, symbol: str) -> bool:
        key = f"{strategy}::{symbol}"
        row = self.conn.execute(
            "SELECT enabled FROM strategy_status WHERE key=?", (key,)
        ).fetchone()
        return row["enabled"] == 1 if row else True  # default enabled

    def _set_enabled(self, strategy: str, symbol: str, enabled: bool, reason: str = ""):
        key = f"{strategy}::{symbol}"
        self.conn.execute(
            """INSERT INTO strategy_status (key, strategy, symbol, enabled, disabled_at, reason)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled,
               disabled_at=excluded.disabled_at, reason=excluded.reason""",
            (key, strategy, symbol, 1 if enabled else 0,
             datetime.now(timezone.utc).isoformat() if not enabled else None, reason),
        )
        self.conn.commit()

        # This table alone was never actually consulted by the scanner —
        # only self_learning.is_strategy_enabled() (which reads
        # strategy_weights.json) gates live signals. Mirror the decision
        # into that file so an auto-disable here actually stops the strategy
        # from firing, not just from showing as disabled in the briefing.
        try:
            weights = _load_strategy_weights()
            weights[key] = 0.0 if not enabled else max(weights.get(key, 1.0), 1.0)
            _save_strategy_weights(weights)
        except Exception as e:
            log.error(f"Failed to sync {key} enabled={enabled} to strategy_weights.json: {e}")

    def run_auto_management(self) -> dict:
        """
        Check all strategy/symbol combos. Disable poor performers,
        re-enable recovered ones. Returns summary dict.
        """
        disabled = []
        enabled  = []

        for strategy in ALL_STRATEGIES:
            for symbol in ALL_MARKETS:
                stats = self._get_stats(strategy, symbol)
                if stats["n"] < MIN_TRADES:
                    continue

                wr         = stats["win_rate"]
                currently  = self.is_enabled(strategy, symbol)
                key        = f"{strategy}/{symbol}"

                if currently and wr < DISABLE_BELOW:
                    self._set_enabled(strategy, symbol, False,
                                      f"WR {wr:.0%} below {DISABLE_BELOW:.0%} threshold")
                    disabled.append(key)
                    log.warning(f"Auto-disabled {key} | WR={wr:.0%}")

                elif not currently and wr >= REENABLE_ABOVE:
                    self._set_enabled(strategy, symbol, True,
                                      f"WR {wr:.0%} recovered above {REENABLE_ABOVE:.0%}")
                    enabled.append(key)
                    log.info(f"Auto-enabled {key} | WR={wr:.0%}")

        return {"disabled": disabled, "enabled": enabled}

    def get_sharpe_rankings(self, market: str) -> list:
        """
        Returns strategies for a market ranked by Sharpe ratio.
        Used by daily_briefing.py.
        """
        results = []
        for strategy in ALL_STRATEGIES:
            stats = self._get_stats(strategy, market)
            results.append({
                "strategy": strategy,
                "symbol":   market,
                "enabled":  self.is_enabled(strategy, market),
                "n":        stats["n"],
                "win_rate": stats["win_rate"],
                "sharpe":   stats["sharpe"],
                "avg_pnl":  stats["avg_pnl"],
            })

        results.sort(key=lambda x: (x["sharpe"] or -999), reverse=True)
        return results

    def get_all_stats(self) -> dict:
        """Full stats dump for all strategy/market combos."""
        out = {}
        for strategy in ALL_STRATEGIES:
            for symbol in ALL_MARKETS:
                stats = self._get_stats(strategy, symbol)
                if stats["n"] > 0:
                    out[f"{strategy}::{symbol}"] = {
                        **stats,
                        "enabled": self.is_enabled(strategy, symbol),
                    }
        return out

    def close(self):
        self.conn.close()
