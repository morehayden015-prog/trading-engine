"""
test_webhook_multi.py — Phase 5 multi-market webhook test
Tests all 4 symbols + all 6 strategies against the live webhook.
"""
import asyncio
import json
import httpx
from datetime import datetime

WEBHOOK_URL = "http://localhost:8000/webhook"

TEST_SIGNALS = [
    {"symbol": "XAUUSD", "direction": "LONG",  "strategy": "sweep_bos_fvg", "price": 2345.50, "timeframe": "5"},
    {"symbol": "XAUUSD", "direction": "SHORT", "strategy": "ict_5step",     "price": 2345.50, "timeframe": "15"},
    {"symbol": "ES",     "direction": "LONG",  "strategy": "rp_profits",    "price": 5280.25, "timeframe": "5"},
    {"symbol": "ES",     "direction": "SHORT", "strategy": "orb_scalp",     "price": 5280.25, "timeframe": "5"},
    {"symbol": "NQ",     "direction": "LONG",  "strategy": "mamba_scalp",   "price": 18450.0, "timeframe": "5"},
    {"symbol": "NQ",     "direction": "SHORT", "strategy": "supply_demand", "price": 18450.0, "timeframe": "15"},
    {"symbol": "CL",     "direction": "LONG",  "strategy": "orb_scalp",     "price": 78.45,   "timeframe": "5"},
    {"symbol": "CL",     "direction": "SHORT", "strategy": "supply_demand", "price": 78.45,   "timeframe": "15"},
    # Edge cases
    {"symbol": "BTCUSD", "direction": "LONG",  "strategy": "sweep_bos_fvg", "price": 65000,   "timeframe": "5"},  # should reject
    {"symbol": "ES",     "direction": "BUY",   "strategy": "rp_profits",    "price": 5280.25, "timeframe": "5"},  # should reject
]


async def send_signal(client: httpx.AsyncClient, signal: dict) -> dict:
    try:
        resp = await client.post(WEBHOOK_URL, json=signal, timeout=30)
        return {"signal": signal, "status": resp.status_code, "response": resp.json()}
    except Exception as e:
        return {"signal": signal, "status": "ERROR", "response": str(e)}


async def run_tests():
    print(f"\n{'='*60}")
    print(f"  Multi-Market Webhook Test | {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*60}\n")

    results = {"passed": 0, "rejected": 0, "errors": 0}

    async with httpx.AsyncClient() as client:
        # Health check first
        try:
            health = await client.get("http://localhost:8000/health", timeout=5)
            print(f"✅ Bot health: {health.json()}\n")
        except Exception as e:
            print(f"❌ Bot not running: {e}")
            return

        for signal in TEST_SIGNALS:
            result = await send_signal(client, signal)
            status   = result["response"].get("status", "unknown") if isinstance(result["response"], dict) else "error"
            trade_id = result["response"].get("trade_id", "")
            score    = result["response"].get("score", "")
            reason   = result["response"].get("reason", "")

            icon = "✅" if status == "accepted" else "🔴" if status == "rejected" else "⚠️"
            print(
                f"{icon} {signal['symbol']:<7} {signal['direction']:<6} | "
                f"strategy={signal['strategy']:<15} | status={status:<10} "
                f"| {f'ID={trade_id} score={score:.2f}' if trade_id else f'reason={reason}'}"
            )

            if status == "accepted":
                results["passed"] += 1
            elif status == "rejected":
                results["rejected"] += 1
            else:
                results["errors"] += 1

            await asyncio.sleep(1)  # rate limit

    print(f"\n{'='*60}")
    print(f"  Results: {results['passed']} accepted | {results['rejected']} rejected | {results['errors']} errors")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(run_tests())
