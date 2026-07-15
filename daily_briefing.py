"""
daily_briefing.py — Daily trading briefing generator
Runs at 8am EST and posts to Discord #market-news
Uses Claude with web search for current market context.
"""
import os
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

log      = logging.getLogger(__name__)
LOG_DIR  = Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

client       = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "10000"))

BRIEFING_SYSTEM = """You are Hayden's personal trading analyst. Every morning you generate a concise, actionable daily briefing.
Structure it exactly like this:

## 🌅 Daily Trading Briefing — {date}

### 📊 Market Overview
[2-3 sentences on overnight price action and key moves]

### 🎯 Today's Watchlist
- **XAUUSD**: [bias + key levels]
- **ES/NQ**: [bias + key levels]  
- **CL**: [bias + key levels]

### ⚠️ Key Events Today
[High-impact news events with times EST]

### 🔍 Trading Bias
[Overall directional bias with reasoning]

### 📋 Strategy Focus
[Which of the 6 strategies are best suited for today's conditions]

### 🚦 Risk Status
[Circuit breaker status and position sizing guidance]

Keep it under 500 words. Be specific with price levels. No fluff."""


def _get_strategy_summary() -> str:
    try:
        from strategy_manager import StrategyManager
        sm      = StrategyManager()
        lines   = []
        for market in ["ES", "NQ", "XAUUSD", "CL"]:
            rankings = sm.get_sharpe_rankings(market)
            enabled  = [r for r in rankings if r["enabled"] and r["win_rate"] is not None]
            if not enabled:
                continue
            top = enabled[0] if enabled else None
            if top:
                lines.append(f"{market}: top={top['strategy']} WR={top['win_rate']:.0%} Sharpe={top['sharpe']:.2f}")
        sm.close()
        return "\n".join(lines) if lines else "No strategy data yet"
    except Exception:
        return "Strategy data unavailable"


def _get_circuit_breaker_summary() -> str:
    try:
        from fee_tracker import FeeTracker
        ft     = FeeTracker(account_size=ACCOUNT_SIZE)
        cb     = ft.get_circuit_breaker_status()
        ft.close()
        status = cb["status"].upper()
        return f"{status}: {cb['reason']} | 7d P&L: ${cb['details']['pnl_7d']:+.2f}"
    except Exception:
        return "Circuit breaker data unavailable"


def _get_performance_summary_text() -> str:
    """TODAY / THIS WEEK / THIS MONTH performance, same numbers as GET /stats."""
    try:
        from paper_executor import PaperExecutor
        pe  = PaperExecutor()
        perf = pe.get_performance_summary()
        pe.close()

        today = perf["today"]
        week  = perf["this_week"]
        month = perf["this_month"]
        wr_str = f"{today['win_rate']:.0%}" if today.get("win_rate") is not None else "N/A"

        return (
            f"**Today:** {today['wins']}W / {today['losses']}L  •  WR {wr_str}  •  ${today['net_pnl']:+.2f}\n"
            f"**This Week:** {week['label']}  •  {week['wins']}W / {week['losses']}L  •  ${week['net_pnl']:+.2f}\n"
            f"**This Month:** {month['label']}  •  {month['wins']}W / {month['losses']}L  •  ${month['net_pnl']:+.2f}"
        )
    except Exception as e:
        log.error(f"Performance summary unavailable: {e}")
        return "Performance summary unavailable"


def _get_bot_updates() -> str:
    try:
        cal_log = LOG_DIR / "calibration_history.jsonl"
        if not cal_log.exists():
            return "No calibration runs yet — bot is still in early learning phase"

        entries = []
        with open(cal_log) as f:
            for line in f:
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass

        if not entries:
            return "No calibration runs yet"

        last    = entries[-1]
        ts      = last.get("timestamp", "")[:10]
        n       = last.get("n_outcomes", 0)
        changes = last.get("changes", {})

        if not changes:
            return f"Last calibration: {ts} ({n} trades analyzed) — no weight changes needed"

        lines = [f"Last calibration: {ts} ({n} trades analyzed)"]
        for comp, ch in list(changes.items())[:3]:
            arrow = "▲" if ch["diff"] > 0 else "▼"
            lines.append(f"  {arrow} {comp}: {ch['old']} → {ch['new']}")

        return "\n".join(lines)
    except Exception:
        return "Bot update data unavailable"


def generate_briefing() -> dict:
    """
    Generates a full daily trading briefing using Claude with web search.
    Returns a dict with all briefing sections.
    """
    today = datetime.now(timezone.utc).strftime("%A, %B %d %Y")

    strategy_summary = _get_strategy_summary()
    cb_summary       = _get_circuit_breaker_summary()
    bot_updates      = _get_bot_updates()

    user_message = f"""Generate today's trading briefing for {today}.

Bot performance context:
{strategy_summary}

Circuit breaker status:
{cb_summary}

Bot calibration updates:
{bot_updates}

Markets to cover: XAUUSD, ES, NQ, CL
Trading strategies available: sweep_bos_fvg, rp_profits, ict_5step, orb_scalp, supply_demand, mamba_scalp

Use web search to get current market prices, overnight moves, and today's economic calendar.
Format exactly as specified in your instructions."""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=BRIEFING_SYSTEM.replace("{date}", today),
            messages=[{"role": "user", "content": user_message}],
        )

        full_text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip()

        return {
            "text":      full_text,
            "date":      today,
            "generated": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        log.error(f"Briefing generation failed: {e}")
        return {
            "text":      f"⚠️ Briefing generation failed: {e}",
            "date":      today,
            "generated": datetime.utcnow().isoformat(),
        }


async def send_daily_briefing():
    """Generate and post the briefing to Discord #market-news."""
    log.info("Generating daily briefing...")
    briefing = generate_briefing()
    perf_text = _get_performance_summary_text()

    try:
        from alerts import send_news_alert
        body = f"{briefing['text'][:1800]}\n\n### 📈 Performance (EST)\n{perf_text}"
        await send_news_alert(
            title=f"🌅 Daily Trading Briefing — {briefing['date']}",
            body=body[:4000],
            color=0xF39C12,
        )
        log.info("Daily briefing sent to Discord")
    except Exception as e:
        log.error(f"Failed to send briefing to Discord: {e}")

    # Archive to log
    archive = LOG_DIR / "briefings.jsonl"
    with open(archive, "a") as f:
        f.write(json.dumps(briefing) + "\n")


async def briefing_scheduler():
    """
    Runs indefinitely. Fires daily briefing at 8:00 AM EST (13:00 UTC).
    """
    last_sent = None
    while True:
        await asyncio.sleep(60)
        now = datetime.now(timezone.utc)
        today_key = now.strftime("%Y-%m-%d")

        # 8am EST = 13:00 UTC (accounting for EDT = UTC-4)
        if now.hour == 13 and now.minute < 5 and today_key != last_sent:
            await send_daily_briefing()
            last_sent = today_key


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(send_daily_briefing())
