"""
myfxbook.py — MyFXBook sentiment + account stats integration
Provides community outlook sentiment for signal filtering.
"""
import os
import logging
import httpx
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

MYFXBOOK_EMAIL    = os.getenv("MYFXBOOK_EMAIL", "")
MYFXBOOK_PASSWORD = os.getenv("MYFXBOOK_PASSWORD", "")

BASE_URL = "https://www.myfxbook.com/api"

# In-memory session cache
_session   = None
_session_ts = None
SESSION_TTL = 3600  # 1 hour

# Sentiment cache
_sentiment_cache: dict = {}
SENTIMENT_TTL = 600  # 10 min


def _get_session() -> str | None:
    global _session, _session_ts
    now = datetime.now(timezone.utc)

    if _session and _session_ts and (now - _session_ts).seconds < SESSION_TTL:
        return _session

    if not MYFXBOOK_EMAIL or not MYFXBOOK_PASSWORD:
        return None

    try:
        resp = httpx.get(
            f"{BASE_URL}/login.json",
            params={"email": MYFXBOOK_EMAIL, "password": MYFXBOOK_PASSWORD},
            timeout=8,
        )
        data = resp.json()
        if not data.get("error"):
            _session    = data["session"]
            _session_ts = now
            return _session
    except Exception as e:
        log.warning(f"MyFXBook login failed: {e}")

    return None


def get_community_outlook(symbol: str) -> dict:
    """
    Get community long/short sentiment for a symbol.
    Returns {"long_pct": float, "short_pct": float, "contrarian_signal": str}
    """
    now = datetime.now(timezone.utc)

    if symbol in _sentiment_cache:
        entry = _sentiment_cache[symbol]
        if (now - entry["ts"]).seconds < SENTIMENT_TTL:
            return entry["data"]

    session = _get_session()
    if not session:
        return {"long_pct": 50.0, "short_pct": 50.0, "contrarian_signal": "NEUTRAL", "available": False}

    # Map symbol to MyFXBook format
    symbol_map = {
        "XAUUSD": "XAU/USD",
        "ES":     "US500",
        "NQ":     "NAS100",
        "CL":     "OIL",
    }
    mfx_symbol = symbol_map.get(symbol, symbol)

    try:
        resp = httpx.get(
            f"{BASE_URL}/get-community-outlook.json",
            params={"session": session, "symbol": mfx_symbol},
            timeout=8,
        )
        data = resp.json()

        if data.get("error"):
            raise Exception(data.get("message", "Unknown error"))

        symbols = data.get("symbols", {}).get("symbol", [])
        if not isinstance(symbols, list):
            symbols = [symbols]

        for s in symbols:
            if s.get("name", "").replace("/", "") == symbol.replace("/", ""):
                long_pct  = float(s.get("longPercentage", 50))
                short_pct = float(s.get("shortPercentage", 50))

                # Contrarian signal: if >70% retail long → consider SHORT (they're usually wrong)
                if long_pct >= 70:
                    contrarian = "CONSIDER_SHORT"
                elif short_pct >= 70:
                    contrarian = "CONSIDER_LONG"
                else:
                    contrarian = "NEUTRAL"

                result = {
                    "long_pct":          round(long_pct, 1),
                    "short_pct":         round(short_pct, 1),
                    "contrarian_signal": contrarian,
                    "available":         True,
                }
                _sentiment_cache[symbol] = {"data": result, "ts": now}
                return result

    except Exception as e:
        log.warning(f"MyFXBook sentiment error for {symbol}: {e}")

    default = {"long_pct": 50.0, "short_pct": 50.0, "contrarian_signal": "NEUTRAL", "available": False}
    _sentiment_cache[symbol] = {"data": default, "ts": now}
    return default
