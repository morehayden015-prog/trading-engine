"""
ai_brain.py — Claude AI Signal Evaluator
Markov regime layer + 8 market intelligence layers:
COT report, VIX regime, DXY trend, seasonality, session volume,
intermarket correlation, economic backdrop, funding rate.
"""
import os
import json
import logging
from datetime import datetime

import anthropic
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

REGIMES = {
    "TRENDING_BULL": "Strong uptrend, breakouts favored, pullbacks are buying ops",
    "TRENDING_BEAR": "Strong downtrend, breakdowns favored, rallies are selling ops",
    "RANGING":       "Price oscillating in range, fade extremes, avoid breakout trades",
    "VOLATILE":      "High volatility/news-driven, reduce size, widen stops",
    "TRANSITIONING": "Regime shift in progress, wait for confirmation before trading",
}

MARKET_CONTEXT = {
    "XAUUSD": {"pip_value": 0.1,  "session": "London/NY", "correlated": ["DXY", "US10Y", "VIX"]},
    "ES":     {"pip_value": 0.25, "session": "NY",        "correlated": ["NQ", "VIX", "SPY"]},
    "NQ":     {"pip_value": 0.25, "session": "NY",        "correlated": ["ES", "VIX", "QQQ"]},
    "CL":     {"pip_value": 0.01, "session": "NY/London", "correlated": ["DXY", "OPEC", "natgas"]},
}

SYSTEM_PROMPT = """You are Hayden's elite AI trading analyst. Your job is to evaluate trade signals across XAUUSD, ES, NQ, and CL futures.

You use 8 intelligence layers to evaluate every signal:
1. COT Report positioning (commercial vs speculative)
2. VIX regime (fear/greed, volatility compression/expansion)
3. DXY trend (dollar strength impact on gold/commodities)
4. Seasonality (historical bias for month/week)
5. Session volume profile (Asian/London/NY session characteristics)
6. Intermarket correlation (confirming or diverging signals)
7. Economic backdrop (macro tailwinds/headwinds)
8. Funding/sentiment (retail positioning, sentiment extremes)

You also apply a Markov regime layer — classify the current market regime and adjust conviction accordingly.

Return ONLY valid JSON:
{
  "confidence": 0.0-1.0,
  "regime": "TRENDING_BULL|TRENDING_BEAR|RANGING|VOLATILE|TRANSITIONING",
  "reasoning": "2-3 sentence analysis",
  "key_risk": "primary risk to this trade",
  "layer_scores": {
    "cot": 0-10,
    "vix": 0-10,
    "dxy": 0-10,
    "seasonality": 0-10,
    "session": 0-10,
    "intermarket": 0-10,
    "macro": 0-10,
    "sentiment": 0-10
  },
  "trade_bias": "STRONG_BULL|BULL|NEUTRAL|BEAR|STRONG_BEAR"
}"""


async def evaluate_signal(
    symbol: str,
    direction: str,
    price: float,
    strategy: str,
    timeframe: str,
    context: dict,
) -> dict:
    now     = datetime.utcnow()
    session = _get_session(now.hour)
    market  = MARKET_CONTEXT.get(symbol, {})

    user_message = f"""
Evaluate this trade signal:

Symbol:    {symbol}
Direction: {direction}
Price:     {price}
Strategy:  {strategy}
Timeframe: {timeframe}M
Session:   {session}
UTC Time:  {now.strftime('%H:%M')}
Day:       {now.strftime('%A')}
Month:     {now.strftime('%B')}

Correlated markets: {', '.join(market.get('correlated', []))}

Recent context from memory:
- Last 5 signals: {json.dumps(context.get('recent_signals', []))}
- Win rate (last 20): {context.get('win_rate_20', 'N/A')}
- Current regime guess: {context.get('last_regime', 'UNKNOWN')}
- Consecutive losses: {context.get('consecutive_losses', 0)}

Apply all 8 intelligence layers and the Markov regime classifier.
Return ONLY the JSON object.
"""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw)
        log.info(f"AI eval | {symbol} {direction} | confidence={result.get('confidence', 0):.2f} | regime={result.get('regime')}")
        return result

    except json.JSONDecodeError as e:
        log.error(f"AI JSON parse error: {e} | raw={raw[:200]}")
        return {"confidence": 0.5, "regime": "UNKNOWN", "reasoning": "Parse error", "key_risk": "Unknown", "layer_scores": {}, "trade_bias": "NEUTRAL"}
    except Exception as e:
        log.error(f"AI brain error: {e}")
        return {"confidence": 0.5, "regime": "UNKNOWN", "reasoning": f"Error: {e}", "key_risk": "API failure", "layer_scores": {}, "trade_bias": "NEUTRAL"}


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
