"""
intel_agent.py — Live Market Intelligence Agent
Runs every 15 minutes. Fetches:
  - VIX level (yfinance)
  - Fear & Greed Index (alternative.me)
  - DXY trend (yfinance)
  - Updates shared AgentContext

Other agents read from AgentContext — they never call this directly.
"""
import asyncio
import logging
import httpx
import yfinance as yf

from agent_context import context

log = logging.getLogger(__name__)


def _fetch_vix() -> float | None:
    try:
        vix = yf.Ticker("^VIX")
        price = vix.fast_info.get("lastPrice") or vix.fast_info.get("regularMarketPrice")
        return round(float(price), 2) if price else None
    except Exception as e:
        log.warning(f"VIX fetch failed: {e}")
        return None


def _classify_vix(vix: float | None) -> str:
    if vix is None:
        return "UNKNOWN"
    if vix < 15:
        return "COMPLACENCY"
    elif vix < 20:
        return "NORMAL"
    elif vix < 30:
        return "ELEVATED"
    elif vix < 40:
        return "FEAR"
    else:
        return "PANIC"


def _fetch_dxy_trend() -> str:
    """Classify DXY trend from recent price action."""
    try:
        dxy = yf.Ticker("DX-Y.NYB")
        hist = dxy.history(period="5d", interval="1h")
        if hist is None or hist.empty:
            return "NEUTRAL"
        closes = hist["Close"].dropna().tolist()
        if len(closes) < 6:
            return "NEUTRAL"
        recent = closes[-1]
        prior  = closes[-6]
        pct    = (recent - prior) / prior * 100
        if pct > 0.3:
            return "STRONG"
        elif pct > 0.1:
            return "RISING"
        elif pct < -0.3:
            return "WEAK"
        elif pct < -0.1:
            return "FALLING"
        else:
            return "NEUTRAL"
    except Exception as e:
        log.warning(f"DXY fetch failed: {e}")
        return "NEUTRAL"


async def _fetch_fear_greed() -> tuple[int | None, str]:
    """Fetch CNN Fear & Greed index from alternative.me."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.alternative.me/fng/?limit=1")
            data = r.json()
            value = int(data["data"][0]["value"])
            label = data["data"][0]["value_classification"]
            return value, label
    except Exception as e:
        log.warning(f"Fear & Greed fetch failed: {e}")
        return None, "UNKNOWN"


def _sizing_from_context(vix: float | None, fear_greed: int | None) -> float:
    """
    Calculate a sizing multiplier based on market conditions.
    High fear + high VIX = smaller size. Low fear + low VIX = normal/larger.
    """
    multiplier = 1.0

    if vix is not None:
        if vix > 35:
            multiplier *= 0.6
        elif vix > 25:
            multiplier *= 0.8
        elif vix < 15:
            multiplier *= 1.1

    if fear_greed is not None:
        if fear_greed < 20:      # extreme fear
            multiplier *= 0.85
        elif fear_greed > 80:    # extreme greed
            multiplier *= 0.90
        elif 40 <= fear_greed <= 60:  # neutral — normal sizing
            pass

    return round(min(1.5, max(0.5, multiplier)), 2)


async def intel_agent_loop():
    """
    Background agent — updates market intelligence every 15 minutes.
    """
    log.info("Intel agent started")

    while True:
        try:
            log.info("Intel agent: fetching market data...")

            # Run sync fetches in thread pool
            loop = asyncio.get_event_loop()
            vix       = await loop.run_in_executor(None, _fetch_vix)
            dxy_trend = await loop.run_in_executor(None, _fetch_dxy_trend)
            fear_greed, fear_greed_label = await _fetch_fear_greed()

            vix_regime  = _classify_vix(vix)
            sizing_mult = _sizing_from_context(vix, fear_greed)

            from datetime import datetime, timezone
            await context.update(
                vix=vix,
                vix_regime=vix_regime,
                dxy_trend=dxy_trend,
                fear_greed=fear_greed,
                fear_greed_label=fear_greed_label,
                vix_sizing_multiplier=sizing_mult,
                intel_updated=datetime.now(timezone.utc).isoformat(),
            )

            log.info(
                f"Intel updated | VIX={vix} ({vix_regime}) | "
                f"DXY={dxy_trend} | F&G={fear_greed} ({fear_greed_label}) | "
                f"Sizing={sizing_mult}x"
            )

            # Post to Discord if conditions are extreme
            if vix and vix > 30:
                try:
                    from alerts import send_bot_update
                    await send_bot_update(
                        "⚠️ High VIX Alert",
                        f"VIX is at **{vix}** ({vix_regime})\n"
                        f"Position sizing reduced to **{sizing_mult}x**\n"
                        f"Fear & Greed: {fear_greed} ({fear_greed_label})",
                    )
                except Exception:
                    pass

        except Exception as e:
            log.error(f"Intel agent error: {e}")

        await asyncio.sleep(900)  # 15 minutes
