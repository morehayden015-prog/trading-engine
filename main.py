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

    # AI brain evaluation
    context = {
        "open_trades": executor.get_open_count(),
        "mode": MODE,
        "phase": PHASE,
    }
    try:
        ai_result = await evaluate_signal(symbol, direction, price, strategy, timeframe, context)
        ai_confidence = ai_result.get("confidence", 0.5)
    except Exception as e:
        log.warning(f"AI brain failed: {e}")
        ai_result = {"confidence": 0.5, "regime": "unknown", "reasoning": str(e)}
        ai_confidence = 0.5

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
