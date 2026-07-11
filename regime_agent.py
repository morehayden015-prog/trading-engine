"""
regime_agent.py — Market Regime Classification Agent
Runs every 30 minutes. Uses Claude with web search to classify
the current market regime across all 4 markets.

Updates AgentContext with:
  - regime (global)
  - trade_bias
  - symbol_bias (per market)
  - regime_reasoning

All other agents read from AgentContext.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import anthropic
import os

from agent_context import context

log = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

REGIME_SYSTEM = """You are an elite market regime classifier for a trading bot.

Your job: classify the current market regime for XAUUSD, ES, NQ, and CL futures.

Regimes:
- TRENDING_BULL: Strong uptrend, momentum favors longs
- TRENDING_BEAR: Strong downtrend, momentum favors shorts
- RANGING: Price oscillating, fade extremes
- VOLATILE: News-driven, unpredictable, reduce size
- TRANSITIONING: Regime shift in progress, wait for confirmation

Use web search to check:
1. Current price levels and recent moves
2. Key economic data/news from last 24h
3. VIX level and trend
4. Dollar strength (DXY)
5. Overall market sentiment

Return ONLY valid JSON:
{
  "global_regime": "TRENDING_BULL|TRENDING_BEAR|RANGING|VOLATILE|TRANSITIONING",
  "trade_bias": "STRONG_BULL|BULL|NEUTRAL|BEAR|STRONG_BEAR",
  "symbol_bias": {
    "XAUUSD": "BULL|BEAR|NEUTRAL",
    "ES": "BULL|BEAR|NEUTRAL",
    "NQ": "BULL|BEAR|NEUTRAL",
    "CL": "BULL|BEAR|NEUTRAL"
  },
  "reasoning": "2-3 sentence summary of current conditions",
  "key_risk": "biggest market risk right now",
  "confidence": 0.0-1.0
}"""


async def classify_regime() -> dict:
    """Ask Claude to classify current market regime using web search."""
    now = datetime.now(timezone.utc)

    # Pull current intel context
    ctx = context.snapshot()
    vix       = ctx.get("vix", "unknown")
    dxy_trend = ctx.get("dxy_trend", "unknown")
    fear_greed = ctx.get("fear_greed", "unknown")

    user_msg = f"""Classify the current market regime.

Current time: {now.strftime('%A %B %d %Y %H:%M UTC')}

Available context:
- VIX: {vix} ({ctx.get('vix_regime', 'unknown')})
- DXY trend: {dxy_trend}
- Fear & Greed: {fear_greed} ({ctx.get('fear_greed_label', 'unknown')})

Use web search to check current prices and news for XAUUSD, ES, NQ, CL.
Return ONLY the JSON object."""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=600,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=REGIME_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )

        # Extract text blocks
        raw = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw += block.text

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw)
        log.info(
            f"Regime classified: {result.get('global_regime')} | "
            f"bias={result.get('trade_bias')} | conf={result.get('confidence', 0):.0%}"
        )
        return result

    except Exception as e:
        log.error(f"Regime agent error: {e}")
        return {
            "global_regime": "UNKNOWN",
            "trade_bias":    "NEUTRAL",
            "symbol_bias":   {"XAUUSD": "NEUTRAL", "ES": "NEUTRAL", "NQ": "NEUTRAL", "CL": "NEUTRAL"},
            "reasoning":     f"Classification failed: {e}",
            "key_risk":      "Unknown",
            "confidence":    0.0,
        }


async def regime_agent_loop():
    """
    Background agent — classifies market regime every 30 minutes.
    Alerts Discord on significant regime changes.
    """
    log.info("Regime agent started")
    last_regime = None

    while True:
        try:
            result = await classify_regime()

            new_regime = result.get("global_regime", "UNKNOWN")
            bias       = result.get("trade_bias", "NEUTRAL")
            reasoning  = result.get("reasoning", "")
            symbol_bias = result.get("symbol_bias", {})

            await context.update(
                regime=new_regime,
                trade_bias=bias,
                regime_reasoning=reasoning,
                regime_updated=datetime.now(timezone.utc).isoformat(),
                symbol_bias=symbol_bias,
            )

            # Alert on regime change
            if last_regime and last_regime != new_regime:
                try:
                    from alerts import send_bot_update
                    regime_emoji = {
                        "TRENDING_BULL":  "🟢",
                        "TRENDING_BEAR":  "🔴",
                        "RANGING":        "🟡",
                        "VOLATILE":       "⚠️",
                        "TRANSITIONING":  "🔄",
                    }.get(new_regime, "❓")

                    await send_bot_update(
                        f"{regime_emoji} Regime Change: {last_regime} → {new_regime}",
                        f"**Bias:** {bias}\n\n"
                        f"**Analysis:** {reasoning}\n\n"
                        f"**Key Risk:** {result.get('key_risk', 'N/A')}\n\n"
                        f"**Symbol Biases:**\n" +
                        "\n".join(f"  • {sym}: {b}" for sym, b in symbol_bias.items()),
                    )
                except Exception as e:
                    log.error(f"Regime alert failed: {e}")

            last_regime = new_regime

        except Exception as e:
            log.error(f"Regime agent loop error: {e}")

        await asyncio.sleep(1800)  # 30 minutes
