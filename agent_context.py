"""
agent_context.py — Shared Agent Context Store
Single source of truth all agents read from and write to.
Thread-safe singleton updated by intel_agent and regime_agent.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


class AgentContext:
    """
    Shared context store for the entire agent ecosystem.
    All agents read from this — no agent calls another agent directly.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self._lock = asyncio.Lock()
        self._data = {
            # Market regime (set by regime_agent)
            "regime":           "UNKNOWN",
            "regime_updated":   None,
            "regime_reasoning": "",
            "trade_bias":       "NEUTRAL",

            # Live market intelligence (set by intel_agent)
            "vix":              None,
            "vix_regime":       "UNKNOWN",
            "dxy_trend":        "NEUTRAL",
            "fear_greed":       None,       # 0-100
            "fear_greed_label": "UNKNOWN",
            "intel_updated":    None,
            # Raw VIX/fear-greed based multiplier — an *input* to risk_agent,
            # kept separate from the final blended "sizing_multiplier" below
            # so the two agents don't feed off each other's output.
            "vix_sizing_multiplier": 1.0,

            # Risk state (set by risk_agent)
            "daily_pnl":        0.0,
            "weekly_pnl":       0.0,
            "open_count":       0,
            "consecutive_losses": 0,
            "sizing_multiplier": 1.0,       # 0.5–1.5x base risk — risk_agent's final output

            # Per-symbol regime overrides — every symbol the bot trades
            # (futures + forex majors), not just the futures desk.
            "symbol_bias": {
                "XAUUSD": "NEUTRAL",
                "ES":     "NEUTRAL",
                "NQ":     "NEUTRAL",
                "CL":     "NEUTRAL",
                "EURUSD": "NEUTRAL",
                "GBPUSD": "NEUTRAL",
                "USDJPY": "NEUTRAL",
                "AUDUSD": "NEUTRAL",
            },
        }

    async def update(self, **kwargs):
        async with self._lock:
            for key, value in kwargs.items():
                if key in self._data:
                    self._data[key] = value
                elif key == "symbol_bias" and isinstance(value, dict):
                    self._data["symbol_bias"].update(value)
            log.debug(f"Context updated: {list(kwargs.keys())}")

    def get(self, key, default=None):
        return self._data.get(key, default)

    def snapshot(self) -> dict:
        """Return full context snapshot for AI brain enrichment."""
        return dict(self._data)

    def get_ai_context(self, symbol: str) -> dict:
        """
        Return context dict formatted for ai_brain.evaluate_signal().
        Merges global regime with symbol-specific bias.
        """
        return {
            "regime":             self._data["regime"],
            "trade_bias":         self._data["trade_bias"],
            "vix":                self._data["vix"],
            "vix_regime":         self._data["vix_regime"],
            "dxy_trend":          self._data["dxy_trend"],
            "fear_greed":         self._data["fear_greed"],
            "fear_greed_label":   self._data["fear_greed_label"],
            "symbol_bias":        self._data["symbol_bias"].get(symbol, "NEUTRAL"),
            "sizing_multiplier":  self._data["sizing_multiplier"],
            "consecutive_losses": self._data["consecutive_losses"],
        }


# Global singleton
context = AgentContext()
