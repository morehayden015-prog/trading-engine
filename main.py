"""
main.py — Hayden Multi-Market Trading Bot
Webhook server entry point for XAUUSD, ES, NQ, CL
MODE=paper | PHASE=2
"""
import os
import json
import asyncio
import logging
import hmac
import hashlib
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from ai_brain import evaluate_signal
from trade_scoring import score_signal
from alerts import send_trade_alert
from paper_executor import PaperExecutor
from memory import Memory
from news_checker import is_news_blackout
from scanner import start_scanner
from fee_tracker import FeeTracker
from outcome_labeler import OutcomeLabeler
from auto_calibrate import calibration_loop
from daily_briefing import briefing_scheduler
from auto_labeler import auto_label_loop
from intel_agent import intel_agent_loop
from market_hours import is_market_open
from regime_agent import regime_agent_loop
from risk_agent import risk_agent_loop, check_correlation_risk
from trade_monitor_agent import trade_monitor_agent_loop
from agent_context import context

load_dotenv()

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(f"{LOG_DIR}/bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

MODE            = os.getenv("MODE", "paper").lower()
PHASE           = int(os.getenv("PHASE", "2"))
ALLOWED_SYMBOLS = set(os.getenv("ALLOWED_SYMBOLS", "XAUUSD,ES,NQ,CL").split(","))
MAX_CONCURRENT  = int(os.getenv("MAX_CONCURRENT_POSITIONS", "2"))
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET", "hayden_private_key")

executor = PaperExecutor()
memory   = Memory()
labeler  = OutcomeLabeler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        f"Starting Hayden Multi-Market Bot | MODE={MODE.upper()} | PHASE={PHASE} "
        f"| Symbols={ALLOWED_SYMBOLS} | Port=8000"
    )
    asyncio.create_task(intel_agent_loop())
    asyncio.create_task(regime_agent_loop())
    asyncio.create_task(risk_agent_loop())
    asyncio.create_task(trade_monitor_agent_loop())
    asyncio.create_task(start_scanner())
    asyncio.create_task(calibration_loop())
    asyncio.create_task(briefing_scheduler())
    asyncio.create_task(auto_label_loop(executor, labeler))
    yield
    executor.close()
    memory.close()
    labeler.close()
    log.info("Bot shutdown complete.")


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    try:
        signal = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Secret check — scanner sends secret automatically, TradingView alerts must include it
    if signal.get("secret") != WEBHOOK_SECRET:
        log.warning("Webhook rejected: invalid secret")
        raise HTTPException(status_code=401, detail="Unauthorized")

    symbol = signal.get("symbol", "").upper()
    if symbol not in ALLOWED_SYMBOLS:
        return JSONResponse({"status": "ignored", "reason": f"{symbol} not allowed"})

    # Weekend guard — no AI calls on weekends
    if not is_market_open():
        return JSONResponse({"status": "ignored", "reason": "weekend — markets closed"})

    # News blackout check
    if is_news_blackout(symbol):
        log.info(f"Trade blocked for {symbol}: news blackout active")
        return JSONResponse({"status": "ignored", "reason": "news blackout"})

    # Concurrent position limit
    if executor.get_open_count() >= MAX_CONCURRENT:
        log.info(f"Trade blocked: {executor.get_open_count()}/{MAX_CONCURRENT} positions open")
        return JSONResponse({"status": "ignored", "reason": f"max concurrent positions ({MAX_CONCURRENT}) reached"})

    # Circuit breaker check
    ft = FeeTracker()
    cb = ft.get_circuit_breaker_status()
    ft.close()
    if cb["status"] == "red":
        log.warning(f"Trade blocked by circuit breaker: {cb['reason']}")
        return JSONResponse({"status": "ignored", "reason": f"circuit breaker: {cb['reason']}"})

    direction = signal.get("direction", signal.get("action", "buy"))
    strategy  = signal.get("strategy", "unknown")
    timeframe = signal.get("timeframe", "5m")
    price     = float(signal.get("price", 0))

    # Correlation risk check
    open_positions = executor.get_open_trades()
    corr = check_correlation_risk(symbol, direction, open_positions)
    if not corr["allowed"]:
        return JSONResponse({"status": "ignored", "reason": corr["reason"]})

    # AI brain evaluation — enriched with live agent context
    ai_ctx = {
        "open_trades":        executor.get_open_count(),
        "mode":               MODE,
        "phase":              PHASE,
        **context.get_ai_context(symbol),
        **memory.get_context(symbol),
    }
    try:
        ai_result = await evaluate_signal(symbol, direction, price, strategy, timeframe, ai_ctx)
        ai_confidence = ai_result.get("confidence", 0.5)
    except Exception as e:
        log.warning(f"AI brain failed: {e}")
        ai_result = {"confidence": 0.5, "regime": "unknown", "reasoning": str(e)}
        ai_confidence = 0.5

    # Apply dynamic sizing from risk agent
    sizing_mult = context.get("sizing_multiplier", 1.0)

    # Score signal
    scored     = score_signal(symbol, direction, strategy, timeframe, ai_confidence)
    score_total = scored["total"]
    log.info(f"Signal scored {score_total:.2f} | {symbol} {direction} {strategy} | AI conf={ai_confidence:.2f}")

    if score_total < 6.5:
        return JSONResponse({"status": "ignored", "reason": f"score too low ({score_total:.2f})"})

    # Execute trade
    try:
        trade = executor.open_trade(
            symbol=symbol,
            direction=direction,
            strategy=strategy,
            timeframe=timeframe,
            price=price,
            score=score_total,
            sizing_multiplier=sizing_mult,
        )
        memory.record_signal(
            symbol=symbol,
            direction=direction,
            strategy=strategy,
            timeframe=timeframe,
            score=score_total,
            trade_id=trade.get("trade_id"),
            regime=ai_result.get("regime", "unknown"),
        )
        await send_trade_alert(
            trade=trade,
            score=scored,
            ai_result=ai_result,
        )
        log.info(f"Trade opened: {trade}")
        return JSONResponse({"status": "executed", "trade": trade, "score": score_total})
    except Exception as e:
        log.error(f"Trade execution failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/outcome")
async def outcome(request: Request):
    """Label a trade WIN / LOSS / BE after the fact."""
    body = await request.body()
    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    trade_id = data.get("trade_id")
    result   = data.get("result", "").upper()
    pnl      = float(data.get("pnl", 0))

    if not trade_id or result not in ("WIN", "LOSS", "BE"):
        raise HTTPException(status_code=400, detail="trade_id and result (WIN/LOSS/BE) required")

    try:
        labeler.label_manual(trade_id=trade_id, result=result, exit_price=pnl)
        return JSONResponse({"status": "labelled", "trade_id": trade_id, "result": result})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    ft = FeeTracker()
    cb = ft.get_circuit_breaker_status()
    ft.close()
    return JSONResponse({
        "status":          "ok",
        "mode":            MODE,
        "phase":           PHASE,
        "open_trades":     executor.get_open_count(),
        "circuit_breaker": cb["status"],
        "cb_reason":       cb["reason"],
        "market_open":     is_market_open(),
    })


@app.get("/trades")
async def trades():
    return JSONResponse(executor.get_open_trades())


@app.get("/stats")
async def stats():
    try:
        session_stats = executor.get_session_stats()
        return JSONResponse(session_stats)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/context")
async def agent_context_endpoint():
    """Return the full shared agent context snapshot."""
    return JSONResponse(context.snapshot())


@app.get("/dashboard", response_class=None)
async def dashboard():
    from fastapi.responses import HTMLResponse
    ft    = FeeTracker()
    cb    = ft.get_circuit_breaker_status()
    ft.close()
    stats = executor.get_session_stats()
    ctx   = context.snapshot()
    trades_open = executor.get_open_trades()

    regime = ctx.get("regime", "UNKNOWN")
    regime_color = {
        "TRENDING_BULL":  "#00C851",
        "TRENDING_BEAR":  "#FF4444",
        "RANGING":        "#FFAA00",
        "VOLATILE":       "#FF6600",
        "TRANSITIONING":  "#9B59B6",
        "UNKNOWN":        "#666",
    }.get(regime, "#666")

    cb_color = {"green": "#00C851", "yellow": "#FFAA00", "red": "#FF4444"}.get(cb["status"], "#666")

    open_rows = ""
    for t in trades_open:
        d_color = "#00C851" if t["direction"].upper() in ("BUY","LONG") else "#FF4444"
        open_rows += f"""
        <tr>
            <td>{t['trade_id']}</td>
            <td>{t['symbol']}</td>
            <td style="color:{d_color};font-weight:bold">{t['direction'].upper()}</td>
            <td>{t['strategy'].replace('_',' ').title()}</td>
            <td>{t['entry_price']}</td>
            <td>{t['score']:.1f}</td>
            <td>{t['entry_time'][:16].replace('T',' ')} UTC</td>
        </tr>"""

    win_rate   = f"{stats['win_rate']:.0%}" if stats.get('win_rate') is not None else "N/A"
    pnl_color  = "#00C851" if stats.get("total_pnl", 0) >= 0 else "#FF4444"
    vix        = ctx.get("vix", "—")
    fear_greed = ctx.get("fear_greed", "—")
    fg_label   = ctx.get("fear_greed_label", "")
    dxy        = ctx.get("dxy_trend", "—")
    sizing     = ctx.get("sizing_multiplier", 1.0)
    bias       = ctx.get("trade_bias", "NEUTRAL")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Hayden Bot Dashboard</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d0d0d; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; padding: 20px; }}
  h1 {{ font-size: 1.6rem; color: #fff; margin-bottom: 4px; }}
  .subtitle {{ color: #666; font-size: 0.85rem; margin-bottom: 20px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px; padding: 16px; }}
  .card .label {{ font-size: 0.7rem; color: #888; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }}
  .card .value {{ font-size: 1.4rem; font-weight: bold; }}
  .card .sub {{ font-size: 0.75rem; color: #888; margin-top: 4px; }}
  .section-title {{ font-size: 0.9rem; color: #888; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }}
  table {{ width: 100%; border-collapse: collapse; background: #1a1a1a; border-radius: 10px; overflow: hidden; }}
  th {{ background: #222; color: #888; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 1px; padding: 10px 12px; text-align: left; }}
  td {{ padding: 10px 12px; font-size: 0.85rem; border-bottom: 1px solid #222; }}
  tr:last-child td {{ border-bottom: none; }}
  .badge {{ display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 0.72rem; font-weight: bold; }}
  .refresh {{ color: #444; font-size: 0.72rem; text-align: right; margin-top: 16px; }}
  .sym-biases {{ display: flex; gap: 10px; flex-wrap: wrap; }}
  .sym-badge {{ background: #222; border-radius: 6px; padding: 6px 10px; font-size: 0.78rem; }}
  .sym-badge span {{ font-weight: bold; }}
</style>
</head>
<body>
<h1>🤖 Hayden Trading Bot</h1>
<p class="subtitle">MODE: {MODE.upper()} &nbsp;|&nbsp; PHASE: {PHASE} &nbsp;|&nbsp; Auto-refreshes every 30s</p>

<div class="grid">
  <div class="card">
    <div class="label">Bot Status</div>
    <div class="value" style="color:#00C851">● ONLINE</div>
    <div class="sub">Railway deployment</div>
  </div>
  <div class="card">
    <div class="label">Circuit Breaker</div>
    <div class="value" style="color:{cb_color}">{cb['status'].upper()}</div>
    <div class="sub">{cb['reason'][:40]}</div>
  </div>
  <div class="card">
    <div class="label">Market Regime</div>
    <div class="value" style="color:{regime_color};font-size:1rem">{regime}</div>
    <div class="sub">Bias: {bias}</div>
  </div>
  <div class="card">
    <div class="label">Open Positions</div>
    <div class="value">{len(trades_open)}</div>
    <div class="sub">Max: {MAX_CONCURRENT}</div>
  </div>
  <div class="card">
    <div class="label">Win Rate</div>
    <div class="value">{win_rate}</div>
    <div class="sub">{stats.get('wins',0)}W / {stats.get('losses',0)}L ({stats.get('total',0)} trades)</div>
  </div>
  <div class="card">
    <div class="label">Total P&L</div>
    <div class="value" style="color:{pnl_color}">${stats.get('total_pnl',0):+.2f}</div>
    <div class="sub">Paper trading</div>
  </div>
  <div class="card">
    <div class="label">VIX</div>
    <div class="value">{vix}</div>
    <div class="sub">{ctx.get('vix_regime','—')}</div>
  </div>
  <div class="card">
    <div class="label">Fear & Greed</div>
    <div class="value">{fear_greed}</div>
    <div class="sub">{fg_label}</div>
  </div>
  <div class="card">
    <div class="label">DXY Trend</div>
    <div class="value" style="font-size:1rem">{dxy}</div>
    <div class="sub">Dollar strength</div>
  </div>
  <div class="card">
    <div class="label">Position Sizing</div>
    <div class="value">{sizing}x</div>
    <div class="sub">Dynamic multiplier</div>
  </div>
</div>

<p class="section-title" style="margin-bottom:8px">Symbol Bias</p>
<div class="sym-biases" style="margin-bottom:24px">
{''.join(f'<div class="sym-badge">{sym}: <span style="color:{"#00C851" if b=="BULL" else "#FF4444" if b=="BEAR" else "#888"}">{b}</span></div>' for sym, b in ctx.get("symbol_bias",{}).items())}
</div>

<p class="section-title">Open Trades</p>
{"<p style='color:#555;font-size:0.85rem;margin-top:8px'>No open positions</p>" if not trades_open else f"""
<table>
  <thead>
    <tr><th>ID</th><th>Symbol</th><th>Dir</th><th>Strategy</th><th>Entry</th><th>Score</th><th>Time</th></tr>
  </thead>
  <tbody>{open_rows}</tbody>
</table>"""}

<p class="refresh">Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</p>
</body>
</html>"""
    return HTMLResponse(content=html)
