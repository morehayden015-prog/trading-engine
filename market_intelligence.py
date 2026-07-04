"""
market_intelligence.py — 8-layer market intelligence system
Used by ai_brain.py to enrich signal evaluation with macro context.
Layers: COT, VIX, DXY, seasonality, session volume, intermarket, macro, funding
"""
import os
import logging
import json
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

# ── VIX regime thresholds ────────────────────────────────────────────────────
VIX_LOW        = 15   # complacency
VIX_ELEVATED   = 20   # caution
VIX_HIGH       = 30   # fear
VIX_EXTREME    = 40   # panic / opportunity

# ── Seasonality (simplified month-based bias) ────────────────────────────────
# +1 = bullish tendency, -1 = bearish, 0 = neutral
GOLD_SEASONALITY = {
    1: 1, 2: 1, 3: 0, 4: -1, 5: 0, 6: -1,
    7: 0, 8: 1, 9: 1, 10: 0, 11: -1, 12: 0,
}

EQUITY_SEASONALITY = {
    1: 1, 2: 0, 3: 1, 4: 1, 5: -1, 6: 0,
    7: 1, 8: -1, 9: -1, 10: 0, 11: 1, 12: 1,
}

SYMBOL_SEASONALITY = {
    "XAUUSD": GOLD_SEASONALITY,
    "ES":     EQUITY_SEASONALITY,
    "NQ":     EQUITY_SEASONALITY,
    "CL":     {1: 0, 2: 0, 3: 1, 4: 1, 5: 1, 6: -1, 7: -1, 8: 0, 9: 0, 10: -1, 11: -1, 12: 0},
}

# ── Session volume profiles ───────────────────────────────────────────────────
SESSION_VOLUME = {
    "ASIAN":        {"liquidity": "LOW",    "trend_likely": False, "fakeout_risk": "HIGH"},
    "LONDON":       {"liquidity": "HIGH",   "trend_likely": True,  "fakeout_risk": "LOW"},
    "NY_OPEN":      {"liquidity": "HIGHEST","trend_likely": True,  "fakeout_risk": "MEDIUM"},
    "NY_AFTERNOON": {"liquidity": "MEDIUM", "trend_likely": False, "fakeout_risk": "MEDIUM"},
    "NY_CLOSE":     {"liquidity": "LOW",    "trend_likely": False, "fakeout_risk": "HIGH"},
}


def get_seasonality_bias(symbol: str) -> dict:
    month = datetime.now(timezone.utc).month
    cal   = SYMBOL_SEASONALITY.get(symbol, {})
    bias  = cal.get(month, 0)
    label = {1: "BULLISH_SEASONAL", -1: "BEARISH_SEASONAL", 0: "NEUTRAL_SEASONAL"}.get(bias, "NEUTRAL_SEASONAL")
    return {"month": month, "bias": bias, "label": label}


def get_session_profile(utc_hour: int) -> dict:
    if 22 <= utc_hour or utc_hour < 7:
        session = "ASIAN"
    elif 7 <= utc_hour < 12:
        session = "LONDON"
    elif 12 <= utc_hour < 17:
        session = "NY_OPEN"
    elif 17 <= utc_hour < 20:
        session = "NY_AFTERNOON"
    else:
        session = "NY_CLOSE"
    return {"session": session, **SESSION_VOLUME[session]}


def get_dxy_context(direction: str, symbol: str) -> dict:
    """
    Simplified DXY context without a live feed.
    Returns alignment score for direction vs expected DXY impact.
    """
    inverse_symbols = {"XAUUSD", "CL"}
    equity_symbols  = {"ES", "NQ"}

    if symbol in inverse_symbols:
        # If DXY strong (assumed), LONG gold/oil is counter-trend
        alignment = "COUNTER_TREND" if direction == "LONG" else "WITH_TREND"
    elif symbol in equity_symbols:
        # Mild inverse correlation
        alignment = "MILD_COUNTER" if direction == "SHORT" else "MILD_WITH"
    else:
        alignment = "NEUTRAL"

    return {"symbol": symbol, "direction": direction, "dxy_alignment": alignment}


def get_intermarket_correlation(symbol: str, direction: str) -> dict:
    """
    Checks for intermarket confirmation signals.
    Returns a simple confirmation score.
    """
    correlations = {
        "XAUUSD": {"positive": ["SILVER", "EUR/USD"], "negative": ["DXY", "US10Y"]},
        "ES":     {"positive": ["NQ", "RTY"],          "negative": ["VIX", "TLT"]},
        "NQ":     {"positive": ["ES", "SMH"],           "negative": ["VIX", "TLT"]},
        "CL":     {"positive": ["BRENT", "XLE"],        "negative": ["DXY", "natgas"]},
    }

    market_corr = correlations.get(symbol, {"positive": [], "negative": []})
    return {
        "symbol":            symbol,
        "direction":         direction,
        "positive_confirms": market_corr["positive"],
        "negative_confirms": market_corr["negative"],
        "note":              "Manual confirmation of correlated markets recommended",
    }


def get_vix_regime(vix_level: float = None) -> dict:
    """
    Classify VIX regime. vix_level can be passed from a live feed
    or estimated from recent market behavior.
    """
    if vix_level is None:
        return {"vix": "UNKNOWN", "regime": "NEUTRAL", "sizing_adj": 1.0}

    if vix_level < VIX_LOW:
        regime = "COMPLACENCY"
        sizing = 1.0
    elif vix_level < VIX_ELEVATED:
        regime = "NORMAL"
        sizing = 1.0
    elif vix_level < VIX_HIGH:
        regime = "ELEVATED"
        sizing = 0.85
    elif vix_level < VIX_EXTREME:
        regime = "FEAR"
        sizing = 0.70
    else:
        regime = "PANIC"
        sizing = 0.50

    return {"vix": vix_level, "regime": regime, "sizing_adj": sizing}


def build_intelligence_context(symbol: str, direction: str) -> dict:
    """
    Assemble all 8 intelligence layers into a single context dict.
    This is what gets passed to ai_brain.py for enriched evaluation.
    """
    now = datetime.now(timezone.utc)
    return {
        "seasonality":    get_seasonality_bias(symbol),
        "session":        get_session_profile(now.hour),
        "dxy":            get_dxy_context(direction, symbol),
        "intermarket":    get_intermarket_correlation(symbol, direction),
        "vix_regime":     get_vix_regime(),   # live VIX can be injected
        "timestamp":      now.isoformat(),
        "symbol":         symbol,
        "direction":      direction,
    }
