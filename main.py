"""
main.py — Hayden Multi-Market Trading Bot
Webhook server entry point for XAUUSD, ES, NQ, CL
MODE=paper | PHASE=2
"""
import os
import json
import asyncio
import logging
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

executor = PaperExecutor()
memory   = Memory()

@asynccontextmanager

async def lifespan(app: FastAPI):
    log.info(
        f"Starting Hayden Multi-Market Bot | MODE={MODE.upper()} | PHASE={PHASE} "
        f"| Symbols={ALLOWED_SYMBOLS} | Port=8000"
    )
    asyncio.create_task(start_scanner())
    yield
    executor.close()
    memory.close()
    log.info("Bot shutdown complete.")

app = FastAPI(lifespan=lifespan)



from fastapi import Request
import hmac, hashlib

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "hayden_private_key")

def verify_signature(payload: bytes, sig: str) -> bool:
    expected = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig or "")

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    try:
        signal = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    symbol = signal.get("symbol", "").upper()
    if symbol not in ALLOWED_SYMBOLS:
        return JSONResponse({"status": "ignored", "reason": f"{symbol} not allowed"})

    scored = score_signal(
        signal.get("symbol", "XAUUSD"),
        signal.get("direction", signal.get("action", "buy")),
        signal.get("strategy", "unknown"),
        signal.get("timeframe", "5m"),
        float(signal.get("ai_confidence", 0.7))
    )

    if scored.get("total", 0) >= 6.5:
        trade = executor.open_trade(
            signal.get("symbol", "XAUUSD"),
            signal.get("direction", signal.get("action", "buy")),
            float(signal.get("price", 0)),
            signal.get("strategy", "unknown"),
            float(scored.get("total", 0))
        )
        memory.record_signal(
            signal.get("symbol", "XAUUSD"),
            signal.get("direction", signal.get("action", "buy")),
            signal.get("strategy", "unknown"),
            float(scored.get("total", 0))
        )
        await send_trade_alert(signal, scored, {})
        return JSONResponse({"status": "executed", "trade": trade})

    return JSONResponse({"status": "ignored", "score": scored.get("total", 0)})


@app.get("/health")
async def health():
    return {"status": "ok", "mode": MODE, "phase": PHASE, "open_trades": executor.get_open_count()}


@app.get("/trades")
async def get_trades():
    return {"trades": executor.get_open_trades()}