"""
alerts.py — Discord alert system
3 channels:
  #trade-alerts  — trade signals (DISCORD_WEBHOOK)
  #market-news   — 8am briefings + breaking news (DISCORD_NEWS_WEBHOOK)
  #bot-updates   — calibration reports (DISCORD_BOT_WEBHOOK)
"""
import os
import logging
import httpx
from datetime import datetime

log = logging.getLogger(__name__)

TRADE_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
NEWS_WEBHOOK  = os.getenv("DISCORD_NEWS_WEBHOOK", "")
BOT_WEBHOOK   = os.getenv("DISCORD_BOT_WEBHOOK", "")

DIRECTION_EMOJI = {"LONG": "🟢", "SHORT": "🔴"}
RESULT_EMOJI    = {"WIN": "✅", "LOSS": "❌", "BE": "⚖️"}


async def send_trade_alert(trade: dict, score: dict, ai_result: dict):
    """Send a trade signal alert to #trade-alerts."""
    if not TRADE_WEBHOOK:
        log.warning("DISCORD_WEBHOOK not set — skipping trade alert")
        return

    symbol    = trade.get("symbol", "?")
    direction = trade.get("direction", "?")
    price = round(trade.get("price", trade.get("entry_price", 0)), 2)
    strategy  = trade.get("strategy", "?")
    trade_id  = trade.get("trade_id", "?")
    regime    = ai_result.get("regime", "UNKNOWN")
    reasoning = ai_result.get("reasoning", "")
    key_risk  = ai_result.get("key_risk", "")

    emoji = DIRECTION_EMOJI.get(direction, "⬜")

    embed = {
        "title": f"{emoji} {symbol} {direction} — {strategy.upper()}",
        "color": 0x00FF88 if direction == "LONG" else 0xFF4444,
        "fields": [
            {"name": "Entry Price", "value": str(price), "inline": True},
            {"name": "Score",       "value": f"{score.get('total', 0):.2f}/10", "inline": True},
            {"name": "AI Confidence","value": f"{ai_result.get('confidence', 0):.0%}", "inline": True},
            {"name": "Regime",      "value": regime, "inline": True},
            {"name": "Session",     "value": score.get("session_name", "?"), "inline": True},
            {"name": "Trade ID",    "value": trade_id, "inline": True},
            {"name": "AI Reasoning","value": reasoning[:300] if reasoning else "N/A", "inline": False},
            {"name": "Key Risk",    "value": key_risk[:200] if key_risk else "N/A", "inline": False},
        ],
        "footer": {"text": f"Hayden Multi-Market Bot | {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"},
        "thumbnail": {"url": "https://i.imgur.com/8YmPAhY.png"},
    }

    await _post_embed(TRADE_WEBHOOK, embed)


async def send_news_alert(title: str, body: str, color: int = 0x3498DB):
    """Send to #market-news channel."""
    if not NEWS_WEBHOOK:
        log.warning("DISCORD_NEWS_WEBHOOK not set — skipping news alert")
        return

    embed = {
        "title": title,
        "description": body[:2000],
        "color": color,
        "footer": {"text": f"Hayden Bot News | {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"},
    }
    await _post_embed(NEWS_WEBHOOK, embed)


async def send_bot_update(title: str, body: str):
    """Send to #bot-updates channel."""
    if not BOT_WEBHOOK:
        log.warning("DISCORD_BOT_WEBHOOK not set — skipping bot update")
        return

    embed = {
        "title": f"🤖 {title}",
        "description": body[:2000],
        "color": 0x9B59B6,
        "footer": {"text": f"Hayden Bot | {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"},
    }
    await _post_embed(BOT_WEBHOOK, embed)


async def _post_embed(webhook_url: str, embed: dict):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json={"embeds": [embed]})
            if resp.status_code not in (200, 204):
                log.error(f"Discord webhook error: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.error(f"Discord send error: {e}")
