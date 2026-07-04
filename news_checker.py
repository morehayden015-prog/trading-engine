"""
news_checker.py — 2-layer news blackout system
Layer 1: ForexFactory scraper (scheduled events)
Layer 2: Claude web search for breaking news
"""
import os
import json
import logging
import asyncio
from datetime import datetime, timezone, timedelta

import httpx
import anthropic
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Minutes before/after a high-impact event to block trading
BLACKOUT_BEFORE = int(os.getenv("NEWS_BLACKOUT_BEFORE", "10"))
BLACKOUT_AFTER  = int(os.getenv("NEWS_BLACKOUT_AFTER",  "15"))

# Symbol → relevant ForexFactory currencies
SYMBOL_CURRENCIES = {
    "XAUUSD": ["USD", "XAU"],
    "ES":     ["USD"],
    "NQ":     ["USD"],
    "CL":     ["USD", "OIL"],
}

# Simple in-memory cache: {symbol: {blackout: bool, expires: datetime}}
_cache: dict = {}
CACHE_TTL_SECONDS = 300  # 5 min cache


def is_news_blackout(symbol: str) -> bool:
    """
    Check if trading should be blocked due to upcoming/recent news.
    Uses cache to avoid hammering ForexFactory.
    """
    now = datetime.now(timezone.utc)

    if symbol in _cache:
        entry = _cache[symbol]
        if now < entry["expires"]:
            return entry["blackout"]

    # Check ForexFactory RSS (lightweight)
    try:
        blackout = _check_forexfactory(symbol, now)
    except Exception as e:
        log.warning(f"ForexFactory check failed for {symbol}: {e}")
        blackout = False

    _cache[symbol] = {
        "blackout": blackout,
        "expires":  now + timedelta(seconds=CACHE_TTL_SECONDS),
    }

    return blackout


def _check_forexfactory(symbol: str, now: datetime) -> bool:
    """
    Fetch ForexFactory calendar RSS and check for high-impact events
    within the blackout window.
    """
    currencies = SYMBOL_CURRENCIES.get(symbol, ["USD"])
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    try:
        resp = httpx.get(url, timeout=8)
        if resp.status_code != 200:
            return False

        events = resp.json()
    except Exception:
        return False

    for event in events:
        impact   = event.get("impact", "").lower()
        currency = event.get("currency", "")
        date_str = event.get("date", "")

        if impact not in ("high", "red"):
            continue
        if currency not in currencies:
            continue

        try:
            event_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            continue

        diff_minutes = (event_dt - now).total_seconds() / 60

        if -BLACKOUT_AFTER <= diff_minutes <= BLACKOUT_BEFORE:
            log.warning(f"News blackout: {symbol} | event={event.get('title')} | {diff_minutes:.0f}min")
            return True

    return False


async def check_breaking_news(symbol: str) -> dict:
    """
    Layer 2: Ask Claude to search for breaking news that might impact the symbol.
    Returns {"blackout": bool, "reason": str}
    """
    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=200,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system="You are a trading risk monitor. Check for breaking news that would cause extreme volatility. Respond ONLY with JSON: {\"blackout\": true/false, \"reason\": \"brief reason\"}",
            messages=[{
                "role": "user",
                "content": f"Search for breaking news in the last 30 minutes affecting {symbol} trading. Should we pause trading? Return JSON only."
            }],
        )
        text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        if text.startswith("{"):
            return json.loads(text)
    except Exception as e:
        log.error(f"Breaking news check failed: {e}")

    return {"blackout": False, "reason": "check failed"}
