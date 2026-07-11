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
    asyncio.create_task(start_scanner())
    asyncio.create_task(calibration_loop())
    asyncio.create_task(briefing_scheduler())
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

    # Run AI brain evaluation
    mem_context = memory.get_context(symbol)
    ai_result = await evaluate_signal(
        symbol=symbol,
        direction=direction,
        price=price,
        strategy=strategy,
        timeframe=timeframe,
        context=mem_context,
    )
    ai_confidence = ai_result.get("confidence", 0.5)

    scored = score_signal(
        symbol,
        direction,
        strategy,
        timeframe,
        ai_confidence,
    )

    log.info(
        f"Signal | {symbol} {direction} | strategy={strategy} | "
        f"score={scored.get('total', 0):.2f} | ai={ai_confidence:.2f} | regime={ai_result.get('regime')}"
    )

    if scored.get("total", 0) >= 6.5:
        trade = executor.open_trade(
            symbol,
            direction,
            price,
            strategy,
            float(scored.get("total", 0)),
            ai_reasoning=ai_result.get("reasoning", ""),
        )
        memory.record_signal(
            symbol,
            direction,
            strategy,
            float(scored.get("total", 0)),
            trade_id=trade.get("trade_id"),
            regime=ai_result.get("regime"),
        )
        await send_trade_alert(signal, scored, ai_result)
        return JSONResponse({"status": "executed", "trade": trade})

    return JSONResponse({
        "status": "ignored",
        "score": scored.get("total", 0),
        "ai_confidence": ai_confidence,
        "regime": ai_result.get("regime"),
    })


@app.post("/outcome")
async def label_outcome(request: Request):
    """Label a trade outcome. Body: {trade_id, result, exit_price (optional)}"""
    body = await request.body()
    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if data.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    trade_id   = data.get("trade_id")
    result     = data.get("result", "").upper()
    exit_price = data.get("exit_price")

    if not trade_id or result not in ("WIN", "LOSS", "BE"):
        raise HTTPException(status_code=400, detail="trade_id and result (WIN/LOSS/BE) required")

    success = labeler.label_manual(trade_id, result, exit_price)
    if not success:
        return JSONResponse({"status": "error", "reason": "trade not found or already closed"}, status_code=404)

    memory.record_outcome(trade_id, result)
    log.info(f"Outcome labeled: {trade_id} → {result}")
    return JSONResponse({"status": "ok", "trade_id": trade_id, "result": result})


@app.get("/health")
async def health():
    ft = FeeTracker()
    cb = ft.get_circuit_breaker_status()
    ft.close()
    return {
        "status": "ok",
        "mode": MODE,
        "phase": PHASE,
        "open_trades": executor.get_open_count(),
        "circuit_breaker": cb["status"],
        "cb_reason": cb["reason"],
    }


@app.get("/trades")
async def get_trades():
    return {"trades": executor.get_open_trades()}


@app.get("/stats")
async def get_stats():
    return executor.get_session_stats()