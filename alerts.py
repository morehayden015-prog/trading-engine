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


async def send_trade_alert(trade: dict, score: dict, ai_result: dict):
    """Send a rich trade signal embed to #trade-alerts."""
    if not TRADE_WEBHOOK:
        log.warning("DISCORD_WEBHOOK not set — skipping trade alert")
        return

    symbol    = trade.get("symbol", "?")
    direction = trade.get("direction", "?").upper()
    price     = round(trade.get("price", trade.get("entry_price", 0)), 4)
    strategy  = trade.get("strategy", "?").replace("_", " ").upper()
    trade_id  = trade.get("trade_id", "?")
    risk_pct  = trade.get("risk_pct", "?")
    risk_usd  = trade.get("risk_usd", "?")
    rr        = trade.get("rr", "?")
    score_val = score.get("total", 0)
    session   = score.get("session_name", "?")
    ai_conf   = ai_result.get("confidence", 0)
    regime    = ai_result.get("regime", "UNKNOWN").upper()
    reasoning = ai_result.get("reasoning", "N/A")
    key_risk  = ai_result.get("key_risk", "N/A")

    is_long  = direction in ("BUY", "LONG")
    emoji    = "🟢" if is_long else "🔴"
    color    = 0x00C851 if is_long else 0xFF4444
    dir_label = "LONG" if is_long else "SHORT"

    # Score bar (visual)
    filled = round(score_val / 10 * 10)
    bar    = "█" * filled + "░" * (10 - filled)

    embed = {
        "title": f"{emoji}  {symbol}  •  {dir_label}  •  {strategy}",
        "color": color,
        "fields": [
            {
                "name": "📍 Entry",
                "value": f"**{price}**",
                "inline": True,
            },
            {
                "name": "⚡ Score",
                "value": f"**{score_val:.1f}/10**\n`{bar}`",
                "inline": True,
            },
            {
                "name": "🤖 AI Confidence",
                "value": f"**{ai_conf:.0%}**  •  {regime}",
                "inline": True,
            },
            {
                "name": "💰 Risk",
                "value": f"**{risk_pct}%**  (${risk_usd})",
                "inline": True,
            },
            {
                "name": "🎯 R:R",
                "value": f"**1:{rr}**",
                "inline": True,
            },
            {
                "name": "🕐 Session",
                "value": f"**{session}**",
                "inline": True,
            },
            {
                "name": "🧠 AI Reasoning",
                "value": (reasoning[:300] if reasoning else "N/A"),
                "inline": False,
            },
            {
                "name": "⚠️ Key Risk",
                "value": (key_risk[:200] if key_risk else "N/A"),
                "inline": False,
            },
        ],
        "footer": {
            "text": f"Trade ID: {trade_id}  •  Hayden Bot  •  {datetime.utcnow().strftime('%H:%M UTC')}",
        },
        "timestamp": datetime.utcnow().isoformat(),
    }

    await _post_embed(TRADE_WEBHOOK, embed)


async def send_trade_closed(
    trade_id: str,
    symbol: str,
    result: str,
    exit_price: float,
    pnl: float,
    tp_used: str = "TP1",
    win_rate: float = None,
):
    """Send trade close notification to #trade-alerts."""
    if not TRADE_WEBHOOK:
        return

    emoji   = "✅" if result == "WIN" else "❌" if result == "LOSS" else "⚖️"
    color   = 0x00C851 if result == "WIN" else 0xFF4444 if result == "LOSS" else 0xFFAA00
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    wr_str  = f"{win_rate:.0%}" if win_rate is not None else "N/A"

    tp_labels = {"TP1": "TP1 — Quick Profit", "TP2": "TP2 — Standard", "TP3": "TP3 — Full Run"}
    tp_label  = tp_labels.get(tp_used, tp_used)

    embed = {
        "title": f"{emoji}  Trade Closed  •  {symbol}  •  {result}",
        "color": color,
        "fields": [
            {"name": "Exit Price", "value": str(exit_price),      "inline": True},
            {"name": "P&L",        "value": f"**{pnl_str}**",     "inline": True},
            {"name": "Target Hit", "value": f"**{tp_label}**",    "inline": True},
            {"name": "Strategy WR","value": wr_str,               "inline": True},
        ],
        "footer": {
            "text": f"Trade ID: {trade_id}  •  Hayden Bot  •  {datetime.utcnow().strftime('%H:%M UTC')}",
        },
        "timestamp": datetime.utcnow().isoformat(),
    }

    await _post_embed(TRADE_WEBHOOK, embed)


async def send_news_alert(title: str, body: str, color: int = 0x3498DB):
    """Send to #market-news channel."""
    if not NEWS_WEBHOOK:
        log.warning("DISCORD_NEWS_WEBHOOK not set — skipping news alert")
        return

    embed = {
        "title": title,
        "description": body[:4000],
        "color": color,
        "footer": {
            "text": f"Hayden Bot  •  {datetime.utcnow().strftime('%A %b %d, %Y  %H:%M UTC')}",
        },
        "timestamp": datetime.utcnow().isoformat(),
    }
    await _post_embed(NEWS_WEBHOOK, embed)


async def send_bot_update(title: str, body: str):
    """Send to #bot-updates channel."""
    if not BOT_WEBHOOK:
        log.warning("DISCORD_BOT_WEBHOOK not set — skipping bot update")
        return

    embed = {
        "title": f"🤖  {title}",
        "description": body[:4000],
        "color": 0x9B59B6,
        "footer": {
            "text": f"Hayden Bot  •  {datetime.utcnow().strftime('%H:%M UTC')}",
        },
        "timestamp": datetime.utcnow().isoformat(),
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
