"""
trade_scoring.py — Multi-market signal scoring
Scores signals across XAUUSD, ES, NQ, CL on:
strategy fit, session timing, DXY alignment, AI confidence, strategy performance tier
"""
import os
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Session scoring by symbol ─────────────────────────────────────────────────
SESSION_SCORES = {
    "XAUUSD": {"ASIAN": 6, "LONDON": 9, "NY_OPEN": 9, "NY_AFTERNOON": 6, "NY_CLOSE": 4},
    "ES":     {"ASIAN": 3, "LONDON": 5, "NY_OPEN": 10, "NY_AFTERNOON": 8, "NY_CLOSE": 5},
    "NQ":     {"ASIAN": 3, "LONDON": 5, "NY_OPEN": 10, "NY_AFTERNOON": 8, "NY_CLOSE": 5},
    "CL":     {"ASIAN": 4, "LONDON": 8, "NY_OPEN": 9,  "NY_AFTERNOON": 7, "NY_CLOSE": 4},
}

# ── Strategy base scores ──────────────────────────────────────────────────────
STRATEGY_BASE = {
    "sweep_bos_fvg":    8.0,
    "rp_profits":       7.5,
    "ict_5step":        8.5,
    "orb_scalp":        7.0,
    "supply_demand":    7.5,
    "mamba_scalp":      8.0,
}

# ── Strategy best markets ─────────────────────────────────────────────────────
STRATEGY_MARKETS = {
    "sweep_bos_fvg":  ["XAUUSD"],
    "rp_profits":     ["ES", "NQ", "XAUUSD"],
    "ict_5step":      ["ES", "NQ", "XAUUSD"],
    "orb_scalp":      ["ES", "NQ", "CL"],
    "supply_demand":  ["XAUUSD", "CL", "ES", "NQ"],
    "mamba_scalp":    ["NQ", "ES"],
}

# ── Timeframe scoring ─────────────────────────────────────────────────────────
TIMEFRAME_SCORES = {
    "1": 5, "3": 6, "5": 8, "15": 9, "30": 8, "60": 7, "240": 6,
}


def score_signal(
    symbol: str,
    direction: str,
    strategy: str,
    timeframe: str,
    ai_confidence: float,
    strategy_win_rate: float = None,
) -> dict:
    """
    Score a trade signal 0–10. Returns full breakdown dict.
    """
    now     = datetime.now(timezone.utc)
    session = _get_session(now.hour)

    # Session score (0–10)
    session_map = SESSION_SCORES.get(symbol, SESSION_SCORES["ES"])
    session_score = session_map.get(session, 5)

    # Strategy base score (0–10)
    strategy_score = STRATEGY_BASE.get(strategy, 6.0)

    # Market fit bonus
    best_markets = STRATEGY_MARKETS.get(strategy, [])
    market_fit   = 1.0 if symbol in best_markets else 0.7

    # Timeframe score (0–10)
    tf_score = TIMEFRAME_SCORES.get(str(timeframe), 6)

    # AI confidence (0–10)
    ai_score = round(ai_confidence * 10, 2)

    # Strategy performance tier (if win rate available)
    perf_multiplier = 1.0
    if strategy_win_rate is not None:
        if strategy_win_rate >= 0.60:
            perf_multiplier = 1.2   # top tier
        elif strategy_win_rate >= 0.50:
            perf_multiplier = 1.0   # normal
        elif strategy_win_rate >= 0.40:
            perf_multiplier = 0.85  # warning
        else:
            perf_multiplier = 0.0   # auto-disable

    # Weighted total
    raw_total = (
        session_score  * 0.20 +
        strategy_score * 0.25 +
        tf_score       * 0.15 +
        ai_score       * 0.40
    )

    total = round(raw_total * market_fit * perf_multiplier, 2)
    total = min(total, 10.0)

    log.debug(
        f"Score breakdown | {symbol} {strategy} | session={session_score} strategy={strategy_score} "
        f"tf={tf_score} ai={ai_score} market_fit={market_fit} perf={perf_multiplier} → {total}"
    )

    return {
        "total":           total,
        "session":         session_score,
        "strategy":        strategy_score,
        "timeframe":       tf_score,
        "ai":              ai_score,
        "market_fit":      market_fit,
        "perf_multiplier": perf_multiplier,
        "session_name":    session,
    }


def score_session(session: str, symbol: str) -> int:
    return SESSION_SCORES.get(symbol, SESSION_SCORES["ES"]).get(session, 5)


def score_dxy(direction: str, symbol: str) -> float:
    """
    Simple DXY alignment heuristic.
    Gold and Oil are inversely correlated to DXY.
    Equities are mildly inversely correlated.
    """
    inverse = {"XAUUSD", "CL"}
    if symbol in inverse:
        return 1.1 if direction == "SHORT" else 0.9
    return 1.0


def _get_session(utc_hour: int) -> str:
    if 22 <= utc_hour or utc_hour < 7:
        return "ASIAN"
    elif 7 <= utc_hour < 12:
        return "LONDON"
    elif 12 <= utc_hour < 17:
        return "NY_OPEN"
    elif 17 <= utc_hour < 20:
        return "NY_AFTERNOON"
    else:
        return "NY_CLOSE"
