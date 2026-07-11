"""
auto_labeler.py — Automatic trade outcome labeler
Runs every 60 seconds, fetches live prices via yfinance,
checks TP/SL levels, auto-closes trades and sends Discord alerts.
"""
import asyncio
import logging
import os

log = logging.getLogger(__name__)

# yfinance ticker map
SYMBOL_MAP = {
    "XAUUSD": "GC=F",
    "ES":     "ES=F",
    "NQ":     "NQ=F",
    "CL":     "CL=F",
}


def _get_current_price(symbol: str) -> float | None:
    try:
        import yfinance as yf
        ticker = SYMBOL_MAP.get(symbol)
        if not ticker:
            return None
        data = yf.Ticker(ticker).fast_info
        price = data.get("lastPrice") or data.get("regularMarketPrice")
        return float(price) if price else None
    except Exception as e:
        log.warning(f"Price fetch failed for {symbol}: {e}")
        return None


async def auto_label_loop(executor, labeler):
    """
    Background loop — checks open trades every 60s,
    auto-labels WIN/LOSS when TP1 or SL is hit.
    """
    log.info("Auto-labeler started")

    while True:
        await asyncio.sleep(60)

        try:
            open_trades = executor.get_open_trades()
            if not open_trades:
                continue

            # Get unique symbols with open trades
            symbols = set(t["symbol"] for t in open_trades)

            for symbol in symbols:
                price = _get_current_price(symbol)
                if price is None:
                    log.debug(f"No price for {symbol}, skipping")
                    continue

                labeled = labeler.check_price_based(symbol, price)

                for outcome in labeled:
                    trade_id = outcome["trade_id"]
                    result   = outcome["result"]

                    # Close in executor (calculates P&L)
                    executor.close_trade(trade_id, result, exit_price=price)

                    log.info(f"Auto-labeled: {trade_id} → {result} @ {price}")

                    # Send Discord alert
                    try:
                        from alerts import send_trade_closed
                        # Get P&L from executor
                        import sqlite3
                        db = os.getenv("DB_PATH", "trades.db")
                        conn = sqlite3.connect(db)
                        row = conn.execute(
                            "SELECT pnl FROM paper_trades WHERE trade_id=?", (trade_id,)
                        ).fetchone()
                        conn.close()
                        pnl = row[0] if row and row[0] is not None else 0.0
                        await send_trade_closed(trade_id, symbol, result, price, pnl)
                    except Exception as e:
                        log.error(f"Discord alert failed for {trade_id}: {e}")

        except Exception as e:
            log.error(f"Auto-labeler error: {e}")
