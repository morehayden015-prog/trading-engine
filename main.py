"""
main.py — Hayden Multi-Market Trading Bot
Webhook server entry point for XAUUSD, ES, NQ, CL, EURUSD, GBPUSD, USDJPY, AUDUSD
MODE=paper | PHASE=2
"""
import os
import json
import time
import asyncio
import logging
import hmac
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse
from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

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
from dedupe_lib import (
    backup_db as dupe_backup_db,
    find_duplicate_groups,
    delete_duplicates,
    refresh_strategy_stats,
    build_preview,
)

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
ALLOWED_SYMBOLS = set(os.getenv("ALLOWED_SYMBOLS", "XAUUSD,ES,NQ,CL,EURUSD,GBPUSD,USDJPY,AUDUSD").split(","))
MAX_CONCURRENT  = int(os.getenv("MAX_CONCURRENT_POSITIONS", "2"))
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET", "hayden_private_key")
ADMIN_SECRET    = os.getenv("ADMIN_SECRET", "")

# ── Webhook dedupe (TradingView sometimes double-fires the same alert) ──
DEDUPE_WINDOW_SECONDS = 10
_recent_signals: dict = {}   # fingerprint -> unix timestamp of last accepted signal


def _signal_fingerprint(symbol: str, strategy: str, direction: str, price: float) -> str:
    return f"{symbol}|{strategy}|{direction}|{price}"


def _is_duplicate_signal(fingerprint: str) -> bool:
    """
    Returns True (and does NOT register the signal) if a signal with this
    fingerprint was already accepted within DEDUPE_WINDOW_SECONDS. Otherwise
    registers the fingerprint and returns False. Cleans out stale entries
    on every call so the dict never grows unbounded.
    """
    now = time.time()
    stale = [fp for fp, ts in _recent_signals.items() if now - ts > DEDUPE_WINDOW_SECONDS]
    for fp in stale:
        del _recent_signals[fp]

    if fingerprint in _recent_signals:
        return True

    _recent_signals[fingerprint] = now
    return False


def _check_admin_auth(request: Request):
    token = request.headers.get("X-Admin-Token", "")
    if not ADMIN_SECRET or token != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ── Dashboard passcode auth ──
DASHBOARD_PASSCODE   = os.getenv("DASHBOARD_PASSCODE", "8462")
DASHBOARD_SECRET_KEY = os.getenv("DASHBOARD_SECRET_KEY", "bot-secret-key")
SESSION_COOKIE_NAME  = "dashboard_session"
SESSION_MAX_AGE      = 60 * 60 * 24 * 7  # 7 days
_serializer = URLSafeTimedSerializer(DASHBOARD_SECRET_KEY)


def _make_session_token() -> str:
    return _serializer.dumps({"auth": True})


def _has_valid_session(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return bool(data.get("auth"))


LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SNUTS // LOGIN</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{
  background:#050008; color:#DCC8FF; font-family:'Courier New',monospace;
  min-height:100vh; display:flex; align-items:center; justify-content:center;
}}
.login-box {{
  background:#0D0015; border:1px solid rgba(168,85,247,0.35);
  padding:40px 36px; width:100%; max-width:340px; text-align:center;
}}
.login-title {{
  font-size:1.1rem; letter-spacing:4px; color:#fff; margin-bottom:6px;
  text-shadow:0 0 20px rgba(168,85,247,0.7);
}}
.login-sub {{
  font-size:.65rem; letter-spacing:2px; color:#a855f7; margin-bottom:28px;
}}
input[type="password"] {{
  width:100%; background:#150025; border:1px solid rgba(168,85,247,0.4);
  color:#DCC8FF; font-family:'Courier New',monospace; font-size:1.1rem;
  letter-spacing:6px; text-align:center; padding:12px; margin-bottom:18px;
  outline:none;
}}
input[type="password"]:focus {{ border-color:#a855f7; box-shadow:0 0 10px rgba(168,85,247,0.4); }}
button {{
  width:100%; background:#a855f7; color:#050008; border:none; padding:12px;
  font-family:'Courier New',monospace; font-size:.8rem; letter-spacing:3px;
  font-weight:bold; cursor:pointer;
}}
button:hover {{ background:#c084fc; }}
.error {{ color:#FF4757; font-size:.7rem; letter-spacing:1px; margin-bottom:16px; }}
</style>
</head>
<body>
<div class="login-box">
  <div class="login-title">SNUTS</div>
  <div class="login-sub">ENTER PASSCODE</div>
  {error_html}
  <form method="post" action="/login">
    <input type="password" name="passcode" autofocus autocomplete="off">
    <button type="submit">UNLOCK</button>
  </form>
</div>
</body>
</html>"""


executor = PaperExecutor()
memory   = Memory()
labeler  = OutcomeLabeler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        f"Starting Hayden Multi-Market Bot | MODE={MODE.upper()} | PHASE={PHASE} "
        f"| Symbols={ALLOWED_SYMBOLS} | Port=8000"
    )
    if DASHBOARD_SECRET_KEY == "bot-secret-key":
        log.warning(
            "DASHBOARD_SECRET_KEY is still the default 'bot-secret-key'. "
            "Set a strong DASHBOARD_SECRET_KEY env var to secure dashboard sessions."
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

    direction = signal.get("direction", signal.get("action", "buy"))
    strategy  = signal.get("strategy", "unknown")
    timeframe = signal.get("timeframe", "5m")
    price     = float(signal.get("price", 0))

    # Dedupe — TradingView sometimes fires the same alert twice within seconds,
    # which used to create duplicate trades with different Trade IDs.
    fingerprint = _signal_fingerprint(symbol, strategy, direction, price)
    if _is_duplicate_signal(fingerprint):
        log.warning(
            f"DUPLICATE SIGNAL REJECTED: {symbol} {direction} {strategy} @ {price} "
            f"(matched a signal received within the last {DEDUPE_WINDOW_SECONDS}s)"
        )
        return JSONResponse({"status": "duplicate_ignored"})

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
    """
    Session-level stats plus TODAY / THIS WEEK / THIS MONTH performance
    (day boundaries computed in America/New_York — the account's trading timezone).
    """
    try:
        session_stats = executor.get_session_stats()
        performance   = executor.get_performance_summary()
        return JSONResponse({**session_stats, **performance})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/context")
async def agent_context_endpoint():
    """Return the full shared agent context snapshot."""
    return JSONResponse(context.snapshot())


# ── Admin: live-DB duplicate trade cleanup ──
# Protected by ADMIN_SECRET env var, sent as the X-Admin-Token header.
# Runs the same backup + preview + confirm flow as cleanup_dupes.py, but
# in-process so it operates on the actual Railway persistent volume DB.

@app.post("/admin/cleanup-dupes/preview")
async def admin_cleanup_preview(request: Request):
    """Step 1: backs up trades.db and returns a preview of duplicates. Deletes nothing."""
    _check_admin_auth(request)
    backup_path = dupe_backup_db()
    groups  = find_duplicate_groups()
    preview = build_preview(groups)
    total_pnl_at_risk = round(sum((p["pnl"] or 0) for p in preview), 2)
    log.info(f"Admin cleanup preview: {len(preview)} duplicate trade(s) found | backup={backup_path}")
    return JSONResponse({
        "status":             "preview",
        "backup_path":        backup_path,
        "duplicate_count":    len(preview),
        "trades_to_delete":   preview,
        "total_pnl_at_risk":  total_pnl_at_risk,
        "next_step":          'POST /admin/cleanup-dupes/confirm with body {"confirm": "DELETE"} to proceed',
    })


@app.post("/admin/cleanup-dupes/confirm")
async def admin_cleanup_confirm(request: Request):
    """Step 2: actually deletes the duplicates found by /preview. Requires {"confirm": "DELETE"}."""
    _check_admin_auth(request)
    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if data.get("confirm") != "DELETE":
        raise HTTPException(status_code=400, detail='Must include {"confirm": "DELETE"} in the request body')

    groups  = find_duplicate_groups()
    preview = build_preview(groups)
    if not preview:
        return JSONResponse({"status": "no_duplicates_found"})

    trade_ids = [p["trade_id"] for p in preview]
    total_pnl_removed = delete_duplicates(trade_ids)
    log.warning(
        f"ADMIN CLEANUP: deleted {len(trade_ids)} duplicate trade(s) | "
        f"P&L removed={total_pnl_removed:+.2f}"
    )

    try:
        strategy_summary = refresh_strategy_stats()
    except Exception as e:
        strategy_summary = {"error": str(e)}

    return JSONResponse({
        "status":             "deleted",
        "deleted_count":      len(trade_ids),
        "total_pnl_removed":  total_pnl_removed,
        "strategy_refresh":   strategy_summary,
    })


@app.get("/admin/download-backup")
async def admin_download_backup(request: Request, path: str):
    """Retrieve a trades_backup_*.db file created by the cleanup endpoints above."""
    _check_admin_auth(request)
    backup_dir = os.path.dirname(os.getenv("DB_PATH", "trades.db")) or "."
    filename = os.path.basename(path)
    if not filename.startswith("trades_backup_") or not filename.endswith(".db"):
        raise HTTPException(status_code=400, detail="Invalid backup filename")
    full_path = os.path.join(backup_dir, filename)
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(full_path, filename=filename, media_type="application/octet-stream")


# ── Admin: read-only per-strategy/symbol performance breakdown ──
# Protected by ADMIN_SECRET env var, sent as the {secret} path segment.
# Read-only: never writes to the DB. Mirrors the day-boundary convention
# /stats uses (exit_time compared in America/New_York) so the two agree.

_STRATEGY_BREAKDOWN_TZ = ZoneInfo("America/New_York")


@app.get("/admin/strategy-breakdown/{secret}")
async def admin_strategy_breakdown(secret: str, days: int = 1):
    """
    Read-only per-strategy + per-symbol performance breakdown over the
    trailing `days` days (default 1 = today only, in America/New_York —
    the same trading-day boundary /stats uses). Only CLOSED trades are
    counted; open positions are excluded.
    """
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        db_path = os.getenv("DB_PATH", "trades.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Detect whether an R-multiple column exists on this DB before
        # selecting it — schemas can drift between environments and this
        # endpoint must not hard-fail if the column is absent.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
        r_col = "rr" if "rr" in cols else None

        select_cols = "strategy, symbol, result, pnl, exit_time"
        if r_col:
            select_cols += f", {r_col}"

        rows = conn.execute(
            f"SELECT {select_cols} FROM paper_trades "
            f"WHERE status='CLOSED' AND exit_time IS NOT NULL"
        ).fetchall()
        conn.close()

        now_local   = datetime.now(timezone.utc).astimezone(_STRATEGY_BREAKDOWN_TZ)
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        since       = today_start - timedelta(days=max(days, 1) - 1)

        def _parse_exit_time(ts: str) -> datetime:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_STRATEGY_BREAKDOWN_TZ)

        matched = [r for r in rows if _parse_exit_time(r["exit_time"]) >= since]

        groups = {}
        for r in matched:
            key = (r["strategy"] or "UNKNOWN", r["symbol"] or "UNKNOWN")
            g = groups.setdefault(key, {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "r_values": []})
            g["trades"] += 1
            if r["result"] == "WIN":
                g["wins"] += 1
            elif r["result"] == "LOSS":
                g["losses"] += 1
            g["net_pnl"] += r["pnl"] or 0
            if r_col:
                r_val = r[r_col]
                if r_val is not None:
                    g["r_values"].append(r_val)

        def _build_entry(g: dict, strategy: str = None, symbol: str = None) -> dict:
            trades = g["trades"]
            entry = {}
            if strategy is not None:
                entry["strategy"] = strategy
            if symbol is not None:
                entry["symbol"] = symbol
            entry.update({
                "trades":    trades,
                "wins":      g["wins"],
                "losses":    g["losses"],
                "win_rate":  round((g["wins"] / trades) * 100, 1) if trades else 0.0,
                "net_pnl":   round(g["net_pnl"], 2),
            })
            if r_col:
                entry["avg_r"] = round(sum(g["r_values"]) / len(g["r_values"]), 2) if g["r_values"] else None
            return entry

        breakdown = [
            _build_entry(g, strategy=strategy, symbol=symbol)
            for (strategy, symbol), g in groups.items()
        ]
        breakdown.sort(key=lambda e: e["net_pnl"])

        totals_group = {
            "trades":   sum(g["trades"] for g in groups.values()),
            "wins":     sum(g["wins"] for g in groups.values()),
            "losses":   sum(g["losses"] for g in groups.values()),
            "net_pnl":  sum(g["net_pnl"] for g in groups.values()),
            "r_values": [v for g in groups.values() for v in g["r_values"]],
        }
        totals = _build_entry(totals_group)

        return JSONResponse({
            "days":       days,
            "since":      since.isoformat(),
            "timezone":   "America/New_York",
            "breakdown":  breakdown,
            "totals":     totals,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/login")
async def login_form(request: Request):
    return HTMLResponse(content=LOGIN_PAGE_HTML.format(error_html=""))


@app.post("/login")
async def login_submit(request: Request):
    try:
        form = await request.form()
        passcode = form.get("passcode", "")

        if passcode != DASHBOARD_PASSCODE:
            error_html = '<div class="error">INCORRECT PASSCODE</div>'
            return HTMLResponse(
                content=LOGIN_PAGE_HTML.format(error_html=error_html),
                status_code=401,
            )

        response = RedirectResponse(url="/dashboard", status_code=303)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=_make_session_token(),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return response
    except Exception:
        log.exception("Login handler failed")
        error_html = '<div class="error">LOGIN ERROR &mdash; TRY AGAIN</div>'
        return HTMLResponse(
            content=LOGIN_PAGE_HTML.format(error_html=error_html),
            status_code=500,
        )


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/dashboard", response_class=None)
async def dashboard(request: Request):
    if not _has_valid_session(request):
        return RedirectResponse(url="/login", status_code=303)
    ft          = FeeTracker()
    cb          = ft.get_circuit_breaker_status()
    ft.close()
    stats       = executor.get_session_stats()
    today_stats = executor.get_today_stats()
    margin      = executor.get_margin_status()
    ctx         = context.snapshot()
    trades_open = executor.get_open_trades()

    regime      = ctx.get("regime", "UNKNOWN")
    bias        = ctx.get("trade_bias", "NEUTRAL")
    vix         = ctx.get("vix", "—")
    vix_regime  = ctx.get("vix_regime", "—")
    fear_greed  = ctx.get("fear_greed", "—")
    fg_label    = ctx.get("fear_greed_label", "—")
    dxy         = ctx.get("dxy_trend", "—")
    sizing      = ctx.get("sizing_multiplier", 1.0)
    win_rate    = f"{stats['win_rate']:.0%}" if stats.get("win_rate") is not None else "---"
    total_pnl   = stats.get("total_pnl", 0.0)
    wins        = stats.get("wins", 0)
    losses      = stats.get("losses", 0)
    total_trades= stats.get("total", 0)
    market_open = is_market_open()

    regime_clr  = {"TRENDING_BULL":"#FFD700","TRENDING_BEAR":"#FF4757","RANGING":"#C77DFF","VOLATILE":"#FF6348","TRANSITIONING":"#9B30FF","UNKNOWN":"#4A2060"}.get(regime,"#4A2060")
    cb_clr      = {"green":"#9B30FF","yellow":"#FFD700","red":"#FF4757"}.get(cb["status"],"#4A2060")
    pnl_clr     = "#FFD700" if total_pnl >= 0 else "#FF4757"
    mkt_clr     = "#9B30FF" if market_open else "#FF4757"
    mkt_txt     = "ONLINE" if market_open else "WEEKEND"

    # Margin usage
    margin_used     = margin["margin_used"]
    margin_avail    = margin["margin_available"]
    margin_pct      = margin["margin_used_pct"]
    account_size    = margin["account_size"]
    margin_clr      = "#FF4757" if margin_pct >= 60 else ("#FFD700" if margin_pct >= 30 else "#9B30FF")

    # Today's win/loss bar chart
    t_wins   = today_stats.get("wins", 0)
    t_losses = today_stats.get("losses", 0)
    t_be     = today_stats.get("be", 0)
    t_total  = today_stats.get("total", 0)
    t_pnl    = today_stats.get("total_pnl", 0.0)
    t_pnl_clr = "#FFD700" if t_pnl >= 0 else "#FF4757"
    t_win_rate_str = f"{today_stats['win_rate']:.0%}" if today_stats.get("win_rate") is not None else "---"
    bar_max   = max(t_wins, t_losses, 1)
    win_bar_h  = round((t_wins / bar_max) * 100)
    loss_bar_h = round((t_losses / bar_max) * 100)

    open_rows = ""
    for t in trades_open:
        d      = t["direction"].upper()
        d_clr  = "#9B30FF" if d in ("BUY","LONG") else "#FF4757"
        d_icon = "▲" if d in ("BUY","LONG") else "▼"
        open_rows += f"""<tr>
          <td class="mono accent">{t['trade_id']}</td>
          <td class="mono">{t['symbol']}</td>
          <td class="mono" style="color:{d_clr}">{d_icon} {d}</td>
          <td class="mono dim">{t['strategy'].replace('_',' ').upper()}</td>
          <td class="mono">{t['entry_price']}</td>
          <td class="mono" style="color:#FFD700">{t['score']:.1f}</td>
          <td class="mono dim">{t['entry_time'][:16].replace('T',' ')}</td>
        </tr>"""

    sym_bias_html = ""
    for sym, b in ctx.get("symbol_bias", {}).items():
        bc = "#9B30FF" if b=="BULL" else "#FF4757" if b=="BEAR" else "#4A2060"
        sym_bias_html += f'<div class="sym-node"><span class="sym-name">{sym}</span><span class="sym-val" style="color:{bc}">{b}</span></div>'

    now_str = datetime.utcnow().strftime("%Y.%m.%d  %H:%M:%S UTC")

    # Pre-compute conditional HTML blocks (can't use backslash escapes inside f-string {})
    if not trades_open:
        positions_html = '<div style="color:var(--dim);font-size:.73rem;letter-spacing:2px;padding:16px 0;font-family:Share Tech Mono,monospace">NO ACTIVE POSITIONS</div>'
    else:
        positions_html = (
            '<div class="table-wrap"><table>'
            '<thead><tr><th>ID</th><th>SYMBOL</th><th>DIRECTION</th><th>STRATEGY</th><th>ENTRY</th><th>SCORE</th><th>OPENED</th></tr></thead>'
            f'<tbody>{open_rows}</tbody>'
            '</table></div>'
        )

    if market_open:
        countdown_html = ""
    else:
        countdown_html = """
<div class="section-label">MARKETS REOPEN IN</div>
<div class="countdown-wrap">
  <div class="countdown-num" id="countdown">--:--:--</div>
  <div class="countdown-label">UNTIL SUNDAY 23:00 UTC<br>ALL AGENTS STANDING BY<br>CREDITS CONSERVED</div>
</div>
<script>
function getSecsUntilOpen() {
  var now = new Date(); var day = now.getUTCDay();
  var target = new Date(now);
  if (day === 6) { target.setUTCDate(target.getUTCDate() + 1); }
  target.setUTCHours(23, 0, 0, 0);
  return Math.max(0, Math.floor((target - now) / 1000));
}
function fmt(s) {
  var h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  return [h,m,sec].map(function(x) { return String(x).padStart(2,'0'); }).join(':');
}
function tick() { var el = document.getElementById('countdown'); if(el) el.textContent = fmt(getSecsUntilOpen()); }
tick(); setInterval(tick, 1000);
</script>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>SNUTS // TRADING ENGINE</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&family=Exo+2:wght@300;400;600&display=swap');
/* ── NEW PURPLE/GOLD DASHBOARD ── */

:root {{
  --purple:  #9B30FF;
  --purple2: #C77DFF;
  --gold:    #FFD700;
  --red:     #FF4757;
  --bg:      #050008;
  --bg2:     #0D0015;
  --bg3:     #150025;
  --border:  #3D1060;
  --borderb: rgba(155,48,255,0.25);
  --dim:     #7A4A9A;
  --text:    #DCC8FF;
}}

* {{ box-sizing:border-box; margin:0; padding:0; }}

body {{
  background: var(--bg);
  color: var(--text);
  font-family: 'Exo 2', 'Share Tech Mono', sans-serif;
  min-height: 100vh;
  overflow-x: hidden;
}}

#bg-canvas {{
  position:fixed; top:0; left:0; width:100%; height:100%;
  pointer-events:none; z-index:0;
}}

body::after {{
  content:''; position:fixed; top:0; left:0; right:0; bottom:0;
  background: repeating-linear-gradient(0deg,transparent,transparent 3px,rgba(155,48,255,0.011) 3px,rgba(155,48,255,0.011) 4px);
  pointer-events:none; z-index:9999;
}}

.wrap {{ position:relative; z-index:1; max-width:1400px; margin:0 auto; padding:24px 20px; }}

/* HEADER */
header {{
  display:flex; align-items:center; justify-content:space-between;
  border-bottom:1px solid var(--border); padding-bottom:20px; margin-bottom:32px; position:relative;
}}
header::after {{
  content:''; position:absolute; bottom:-1px; left:0; width:55%; height:1px;
  background:linear-gradient(90deg,var(--purple),transparent);
}}
.logo {{ display:flex; align-items:center; gap:16px; }}
.logo-diamond {{ width:52px; height:52px; flex-shrink:0; }}
.logo-diamond svg {{ width:100%; height:100%; filter:drop-shadow(0 0 14px rgba(155,48,255,0.9)); }}
.logo-text {{ line-height:1.25; }}
.logo-title {{
  font-family:'Orbitron',monospace; font-size:1.55rem; font-weight:900; color:#fff;
  letter-spacing:6px; text-shadow:0 0 30px rgba(155,48,255,0.85),0 0 60px rgba(155,48,255,0.3);
}}
.logo-sub {{ font-size:.6rem; color:var(--gold); letter-spacing:5px; font-family:'Share Tech Mono',monospace; }}
.header-right {{ text-align:right; }}
.clock {{ font-family:'Share Tech Mono',monospace; font-size:.78rem; color:var(--purple2); letter-spacing:2px; }}
.mode-badge {{
  display:inline-block; margin-top:6px; font-size:.58rem; letter-spacing:3px;
  border:1px solid var(--gold); color:var(--gold); padding:3px 10px;
  font-family:'Share Tech Mono',monospace; box-shadow:0 0 8px rgba(255,215,0,0.2);
}}

/* SECTION LABEL */
.section-label {{
  font-family:'Orbitron',monospace; font-size:.67rem; letter-spacing:5px;
  color:var(--gold); margin-bottom:14px; display:flex; align-items:center; gap:12px;
  text-shadow:0 0 10px rgba(255,215,0,0.5);
}}
.section-label::before {{ content:'◈'; color:var(--purple); font-size:.9rem; }}
.section-label::after {{ content:''; flex:1; height:1px; background:linear-gradient(90deg,var(--border),transparent); }}

/* CARDS */
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-bottom:32px; }}
.card {{
  background:var(--bg2); border:1px solid var(--borderb); padding:16px; position:relative;
  clip-path:polygon(0 0,calc(100% - 14px) 0,100% 14px,100% 100%,14px 100%,0 calc(100% - 14px));
  transition:border-color .3s,box-shadow .3s;
}}
.card:hover {{ border-color:rgba(155,48,255,0.55); box-shadow:0 0 20px rgba(155,48,255,0.12) inset; }}
.card::before {{
  content:''; position:absolute; top:-1px; right:-1px; width:17px; height:17px;
  border-top:2px solid var(--gold); border-right:2px solid var(--gold);
}}
.card::after {{
  content:''; position:absolute; bottom:-1px; left:-1px; width:17px; height:17px;
  border-bottom:2px solid var(--purple); border-left:2px solid var(--purple);
}}
.card-label {{ font-size:.63rem; letter-spacing:2px; color:var(--dim); text-transform:uppercase; margin-bottom:10px; font-family:'Share Tech Mono',monospace; }}
.card-value {{ font-family:'Orbitron',monospace; font-size:1.2rem; font-weight:700; line-height:1; }}
.card-sub {{ font-size:.65rem; color:var(--dim); margin-top:8px; letter-spacing:1px; font-family:'Share Tech Mono',monospace; }}

/* MARGIN METER */
.margin-meter-wrap {{ margin-bottom:32px; }}
.margin-meter-track {{
  width:100%; height:10px; background:var(--bg2); border:1px solid var(--borderb);
  overflow:hidden; position:relative;
}}
.margin-meter-fill {{
  height:100%; transition:width .4s ease; box-shadow:0 0 12px currentColor;
}}
.margin-meter-label {{ font-size:.6rem; letter-spacing:2px; margin-top:8px; text-align:right; }}

/* WIN/LOSS BAR CHART */
.chart-wrap {{
  background:var(--bg2); border:1px solid var(--borderb); padding:24px; margin-bottom:32px;
  display:flex; align-items:flex-end; gap:0; position:relative;
  clip-path:polygon(0 0,calc(100% - 16px) 0,100% 16px,100% 100%,0 100%);
}}
.chart-wrap::before {{
  content:''; position:absolute; top:-1px; right:-1px; width:22px; height:22px;
  border-top:2px solid var(--gold); border-right:2px solid var(--gold);
}}
.chart-bars {{ display:flex; align-items:flex-end; gap:36px; height:180px; flex:1; padding-left:8px; }}
.chart-col {{ display:flex; flex-direction:column; align-items:center; justify-content:flex-end; height:100%; width:80px; }}
.chart-bar {{
  width:100%; border-radius:2px 2px 0 0; position:relative;
  transition:height .5s ease; min-height:3px;
  box-shadow:0 0 16px currentColor;
}}
.chart-bar-count {{
  font-family:'Orbitron',monospace; font-weight:900; font-size:1.1rem;
  margin-bottom:8px; text-shadow:0 0 10px currentColor;
}}
.chart-bar-label {{
  font-family:'Share Tech Mono',monospace; font-size:.63rem; letter-spacing:2px;
  color:var(--dim); margin-top:10px;
}}
.chart-side {{
  display:flex; flex-direction:column; gap:14px; padding-left:28px; margin-left:24px;
  border-left:1px solid var(--border); min-width:150px;
}}
.chart-side-label {{ font-size:.6rem; letter-spacing:2px; color:var(--dim); font-family:'Share Tech Mono',monospace; }}
.chart-side-val {{ font-family:'Orbitron',monospace; font-size:1.05rem; font-weight:700; }}

/* SYMBOL BIAS */
.sym-row {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:32px; }}
.sym-node {{
  background:var(--bg2); border:1px solid var(--borderb); padding:10px 18px;
  display:flex; flex-direction:column; align-items:center; gap:4px; min-width:90px; position:relative;
  clip-path:polygon(10px 0,100% 0,100% calc(100% - 10px),calc(100% - 10px) 100%,0 100%,0 10px);
}}
.sym-node::before {{
  content:''; position:absolute; top:-1px; right:-1px; width:13px; height:13px;
  border-top:1px solid var(--gold); border-right:1px solid var(--gold);
}}
.sym-name {{ font-family:'Share Tech Mono',monospace; font-size:.68rem; letter-spacing:3px; color:var(--dim); }}
.sym-val  {{ font-family:'Orbitron',monospace; font-size:.82rem; font-weight:700; letter-spacing:2px; }}

/* TABLE */
.table-wrap {{
  background:var(--bg2); border:1px solid var(--borderb); overflow:hidden; margin-bottom:32px;
  clip-path:polygon(0 0,calc(100% - 20px) 0,100% 20px,100% 100%,0 100%); position:relative;
}}
.table-wrap::before {{
  content:''; position:absolute; top:-1px; right:-1px; width:26px; height:26px;
  border-top:2px solid var(--gold); border-right:2px solid var(--gold);
}}
table {{ width:100%; border-collapse:collapse; }}
th {{
  font-family:'Share Tech Mono',monospace; font-size:.63rem; letter-spacing:2px; text-transform:uppercase;
  color:var(--gold); padding:12px 16px; text-align:left; border-bottom:1px solid var(--border); background:var(--bg3);
}}
td {{ padding:11px 16px; border-bottom:1px solid rgba(61,16,96,0.45); font-family:'Share Tech Mono',monospace; font-size:.83rem; }}
tr:last-child td {{ border-bottom:none; }}
tr:hover td {{ background:rgba(155,48,255,0.05); }}
.mono {{ font-family:'Share Tech Mono',monospace; }}
.accent {{ color:var(--purple2); }}
.dim {{ color:var(--dim); }}

/* AGENT MAP */
.agent-map {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:32px; }}
.agent-node {{
  background:var(--bg2); border:1px solid var(--borderb); padding:18px 14px 14px;
  position:relative; display:flex; flex-direction:column; align-items:center;
  text-align:center; gap:8px; transition:all .3s; cursor:default;
  clip-path:polygon(14px 0,calc(100% - 14px) 0,100% 14px,100% calc(100% - 14px),calc(100% - 14px) 100%,14px 100%,0 calc(100% - 14px),0 14px);
}}
.agent-node:hover {{ border-color:rgba(155,48,255,0.65); box-shadow:0 0 28px rgba(155,48,255,0.18) inset,0 0 20px rgba(155,48,255,0.12); transform:translateY(-2px); }}
.agent-node::before {{
  content:''; position:absolute; top:-1px; right:-1px; width:15px; height:15px;
  border-top:2px solid var(--gold); border-right:2px solid var(--gold);
}}
.agent-node::after {{
  content:''; position:absolute; bottom:-1px; left:-1px; width:15px; height:15px;
  border-bottom:2px solid var(--purple); border-left:2px solid var(--purple);
}}
.agent-ping {{
  position:absolute; top:8px; left:50%; transform:translateX(-50%);
  width:36px; height:2px; background:linear-gradient(90deg,transparent,var(--purple),transparent);
  animation:ping-line 2s ease-in-out infinite;
}}
@keyframes ping-line {{
  0%,100% {{ opacity:1; box-shadow:0 0 8px var(--purple); }}
  50% {{ opacity:.15; box-shadow:none; }}
}}
.agent-icon {{
  width:44px; height:44px; border:1px solid rgba(155,48,255,0.45); background:var(--bg3);
  display:flex; align-items:center; justify-content:center; font-size:1.1rem; color:var(--purple2);
  box-shadow:0 0 14px rgba(155,48,255,0.2) inset; transition:all .3s;
  clip-path:polygon(6px 0,calc(100% - 6px) 0,100% 6px,100% calc(100% - 6px),calc(100% - 6px) 100%,6px 100%,0 calc(100% - 6px),0 6px);
}}
.agent-node:hover .agent-icon {{ color:var(--gold); border-color:var(--gold); box-shadow:0 0 20px rgba(255,215,0,0.3) inset; }}
.agent-name {{ font-family:'Orbitron',monospace; font-size:.7rem; font-weight:700; letter-spacing:3px; color:#fff; text-shadow:0 0 10px rgba(155,48,255,0.5); }}
.agent-status {{ font-size:.62rem; color:var(--dim); line-height:1.55; font-family:'Share Tech Mono',monospace; }}
.agent-freq {{ font-family:'Share Tech Mono',monospace; font-size:.57rem; letter-spacing:2px; color:var(--purple2); opacity:.85; border-top:1px solid rgba(61,16,96,0.55); padding-top:8px; width:100%; text-align:center; }}

/* COUNTDOWN */
.countdown-wrap {{
  background:var(--bg2); border:1px solid var(--borderb); border-left:3px solid var(--gold);
  padding:20px 24px; margin-bottom:32px; display:flex; align-items:center; gap:24px;
  clip-path:polygon(0 0,calc(100% - 16px) 0,100% 16px,100% 100%,0 100%); position:relative;
}}
.countdown-wrap::before {{
  content:''; position:absolute; top:-1px; right:-1px; width:22px; height:22px;
  border-top:2px solid var(--gold); border-right:2px solid var(--gold);
}}
.countdown-num {{
  font-family:'Orbitron',monospace; font-size:2.5rem; font-weight:900; color:var(--gold);
  text-shadow:0 0 30px rgba(255,215,0,0.65),0 0 60px rgba(255,215,0,0.2); letter-spacing:4px;
}}
.countdown-label {{ font-family:'Share Tech Mono',monospace; font-size:.63rem; letter-spacing:2px; color:var(--dim); line-height:1.85; }}

/* FOOTER */
footer {{
  border-top:1px solid var(--border); padding-top:14px;
  display:flex; justify-content:space-between; align-items:center; position:relative;
}}
footer::before {{
  content:''; position:absolute; top:-1px; left:0; width:40%; height:1px;
  background:linear-gradient(90deg,var(--gold),transparent);
}}
.footer-txt {{ font-family:'Share Tech Mono',monospace; font-size:.58rem; color:var(--dim); letter-spacing:2px; }}
.live-dot {{
  display:inline-block; width:6px; height:6px; border-radius:50%; background:var(--purple);
  margin-right:6px; box-shadow:0 0 10px var(--purple); animation:pulse 1.5s infinite; vertical-align:middle;
}}
.glow-purple {{ text-shadow:0 0 14px rgba(155,48,255,.75); }}
.glow-gold   {{ text-shadow:0 0 14px rgba(255,215,0,.75); }}
.glow-red    {{ text-shadow:0 0 14px rgba(255,71,87,.75); }}
@keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:.25; }} }}
@media (max-width:700px) {{
  .agent-map {{ grid-template-columns:repeat(2,1fr); }}
  .grid {{ grid-template-columns:repeat(2,1fr); }}
  .logo-title {{ font-size:1rem; letter-spacing:3px; }}
  .countdown-num {{ font-size:1.8rem; }}
}}
</style>
</head>
<body>
<canvas id="bg-canvas"></canvas>
<div class="wrap">

<!-- HEADER -->
<header>
  <div class="logo">
    <div class="logo-diamond">
      <svg viewBox="0 0 50 50" xmlns="http://www.w3.org/2000/svg">
        <polygon points="25,2 48,25 25,48 2,25" fill="rgba(5,0,8,0.85)" stroke="#9B30FF" stroke-width="1.5"/>
        <polygon points="25,9 41,25 25,41 9,25" fill="none" stroke="#FFD700" stroke-width="0.8" opacity="0.65"/>
        <polygon points="25,17 35,25 25,33 15,25" fill="rgba(155,48,255,0.28)" stroke="#9B30FF" stroke-width="1"/>
        <line x1="2" y1="25" x2="48" y2="25" stroke="#FFD700" stroke-width="0.6" opacity="0.4"/>
        <line x1="25" y1="2" x2="25" y2="48" stroke="#9B30FF" stroke-width="0.6" opacity="0.4"/>
      </svg>
    </div>
    <div class="logo-text">
      <div class="logo-title">SNUTS</div>
      <div class="logo-sub">TRADING ENGINE // HAYDEN CORP</div>
    </div>
  </div>
  <div class="header-right">
    <div class="clock">{now_str}</div>
    <div class="mode-badge">{MODE.upper()} MODE // PHASE {PHASE}</div>
  </div>
</header>

<!-- STATUS CARDS -->
<div class="section-label">SYSTEM STATUS</div>
<div class="grid">
  <div class="card">
    <div class="card-label">System</div>
    <div class="card-value glow-purple" style="color:var(--purple2);font-size:.92rem">● {mkt_txt}</div>
    <div class="card-sub" style="color:{mkt_clr}">MARKETS {"OPEN" if market_open else "CLOSED"}</div>
  </div>
  <div class="card">
    <div class="card-label">Circuit Breaker</div>
    <div class="card-value" style="color:{cb_clr}">{cb['status'].upper()}</div>
    <div class="card-sub">{cb['reason'][:35]}</div>
  </div>
  <div class="card">
    <div class="card-label">Regime</div>
    <div class="card-value" style="color:{regime_clr};font-size:.78rem">{regime.replace('_',' ')}</div>
    <div class="card-sub">BIAS: {bias}</div>
  </div>
  <div class="card">
    <div class="card-label">Open Positions</div>
    <div class="card-value glow-purple" style="color:var(--purple2)">{len(trades_open)}</div>
    <div class="card-sub">MAX: {MAX_CONCURRENT}</div>
  </div>
  <div class="card">
    <div class="card-label">Win Rate</div>
    <div class="card-value glow-gold" style="color:var(--gold)">{win_rate}</div>
    <div class="card-sub">{wins}W · {losses}L · {total_trades}</div>
  </div>
  <div class="card">
    <div class="card-label">Total P&amp;L</div>
    <div class="card-value" style="color:{pnl_clr}">${total_pnl:+.2f}</div>
    <div class="card-sub">PAPER ACCOUNT</div>
  </div>
  <div class="card">
    <div class="card-label">VIX Index</div>
    <div class="card-value" style="color:var(--text)">{vix}</div>
    <div class="card-sub">{vix_regime}</div>
  </div>
  <div class="card">
    <div class="card-label">Fear &amp; Greed</div>
    <div class="card-value glow-gold" style="color:var(--gold)">{fear_greed}</div>
    <div class="card-sub">{fg_label.upper()}</div>
  </div>
  <div class="card">
    <div class="card-label">DXY Trend</div>
    <div class="card-value" style="color:var(--text);font-size:.82rem">{dxy}</div>
    <div class="card-sub">DOLLAR STRENGTH</div>
  </div>
  <div class="card">
    <div class="card-label">Size Multiplier</div>
    <div class="card-value glow-gold" style="color:var(--gold)">{sizing}×</div>
    <div class="card-sub">DYNAMIC RISK</div>
  </div>
  <div class="card">
    <div class="card-label">Margin Used</div>
    <div class="card-value" style="color:{margin_clr}">${margin_used:,.2f}</div>
    <div class="card-sub">{margin_pct:.1f}% OF ${account_size:,.0f}</div>
  </div>
  <div class="card">
    <div class="card-label">Margin Available</div>
    <div class="card-value" style="color:var(--text)">${margin_avail:,.2f}</div>
    <div class="card-sub">{margin['open_positions']} POSITION(S) OPEN</div>
  </div>
</div>

<!-- MARGIN METER -->
<div class="margin-meter-wrap">
  <div class="margin-meter-track">
    <div class="margin-meter-fill" style="width:{min(margin_pct,100)}%;background:{margin_clr}"></div>
  </div>
  <div class="margin-meter-label mono dim">MARGIN UTILIZATION — {margin_pct:.1f}%</div>
</div>

<!-- AGENT MAP -->
<div class="section-label">ACTIVE AGENTS</div>
<div class="agent-map">
  <div class="agent-node">
    <div class="agent-ping"></div>
    <div class="agent-icon">◈</div>
    <div class="agent-name">INTEL</div>
    <div class="agent-status">VIX · DXY<br>FEAR &amp; GREED</div>
    <div class="agent-freq">↻ 15 MIN</div>
  </div>
  <div class="agent-node">
    <div class="agent-ping"></div>
    <div class="agent-icon">⬡</div>
    <div class="agent-name">REGIME</div>
    <div class="agent-status">CLAUDE AI<br>WEB SEARCH</div>
    <div class="agent-freq">↻ 30 MIN</div>
  </div>
  <div class="agent-node">
    <div class="agent-ping"></div>
    <div class="agent-icon">⚠</div>
    <div class="agent-name">RISK</div>
    <div class="agent-status">DRAWDOWN<br>SIZING SCALE</div>
    <div class="agent-freq">↻ 60 SEC</div>
  </div>
  <div class="agent-node">
    <div class="agent-ping"></div>
    <div class="agent-icon">◎</div>
    <div class="agent-name">MONITOR</div>
    <div class="agent-status">BE · PARTIAL<br>TP / SL</div>
    <div class="agent-freq">↻ 30 SEC</div>
  </div>
  <div class="agent-node">
    <div class="agent-ping"></div>
    <div class="agent-icon">⊞</div>
    <div class="agent-name">SCANNER</div>
    <div class="agent-status">16 STRATEGIES<br>8 MARKETS</div>
    <div class="agent-freq">↻ 5 MIN</div>
  </div>
  <div class="agent-node">
    <div class="agent-ping"></div>
    <div class="agent-icon">∿</div>
    <div class="agent-name">CALIBRATE</div>
    <div class="agent-status">SELF-LEARNING<br>WEIGHT ADJ</div>
    <div class="agent-freq">↻ 25 TRADES</div>
  </div>
  <div class="agent-node">
    <div class="agent-ping"></div>
    <div class="agent-icon">☀</div>
    <div class="agent-name">BRIEFING</div>
    <div class="agent-status">DAILY INTEL<br>MARKET SUMMARY</div>
    <div class="agent-freq">↻ 08:00 EST</div>
  </div>
  <div class="agent-node" style="border-color:rgba(61,16,96,0.3);opacity:.4;border-style:dashed">
    <div class="agent-icon" style="border-color:var(--dim);color:var(--dim)">+</div>
    <div class="agent-name" style="color:var(--dim)">EXPAND</div>
    <div class="agent-status">SLOT AVAILABLE</div>
    <div class="agent-freq">OFFLINE</div>
  </div>
</div>

<!-- SYMBOL INTELLIGENCE -->
<div class="section-label">SYMBOL INTELLIGENCE</div>
<div class="sym-row">
{sym_bias_html if sym_bias_html else '<span style="color:var(--dim);font-size:.73rem;font-family:Share Tech Mono,monospace;letter-spacing:2px">AWAITING REGIME CLASSIFICATION...</span>'}
</div>

<!-- TODAY'S PERFORMANCE -->
<div class="section-label">TODAY'S WIN / LOSS</div>
<div class="chart-wrap">
  <div class="chart-bars">
    <div class="chart-col">
      <div class="chart-bar-count" style="color:#9B30FF">{t_wins}</div>
      <div class="chart-bar" style="height:{win_bar_h}%;background:#9B30FF;color:#9B30FF"></div>
      <div class="chart-bar-label">WINS</div>
    </div>
    <div class="chart-col">
      <div class="chart-bar-count" style="color:#FF4757">{t_losses}</div>
      <div class="chart-bar" style="height:{loss_bar_h}%;background:#FF4757;color:#FF4757"></div>
      <div class="chart-bar-label">LOSSES</div>
    </div>
  </div>
  <div class="chart-side">
    <div><div class="chart-side-label">TRADES TODAY</div><div class="chart-side-val" style="color:var(--text)">{t_total}</div></div>
    <div><div class="chart-side-label">BREAKEVEN</div><div class="chart-side-val" style="color:var(--dim)">{t_be}</div></div>
    <div><div class="chart-side-label">WIN RATE</div><div class="chart-side-val glow-gold" style="color:var(--gold)">{t_win_rate_str}</div></div>
    <div><div class="chart-side-label">TODAY P&amp;L</div><div class="chart-side-val" style="color:{t_pnl_clr}">${t_pnl:+.2f}</div></div>
  </div>
</div>

<!-- OPEN POSITIONS -->
<div class="section-label">OPEN POSITIONS</div>
{positions_html}

{countdown_html}

<!-- FOOTER -->
<footer>
  <div class="footer-txt"><span class="live-dot"></span>LIVE · AUTO-REFRESH 30S</div>
  <div class="footer-txt">SNUTS ENGINE v2.0 // {now_str}</div>
</footer>

</div>

<script>
// CANVAS BACKGROUND — purple grid + falling diamonds + light beams
var canvas = document.getElementById('bg-canvas');
var c = canvas.getContext('2d');
var W, H;
function resize() {{ W = canvas.width = window.innerWidth; H = canvas.height = window.innerHeight; }}
resize();
window.addEventListener('resize', resize);

// ── REALISTIC BRILLIANT-CUT DIAMOND ──
function Diamond(spread) {{ this.reset(spread); }}
Diamond.prototype.reset = function(spread) {{
  this.x = Math.random() * W;
  this.y = spread ? Math.random() * H : -80 - Math.random() * 300;
  this.s = 18 + Math.random() * 40;
  this.vy = 0.16 + Math.random() * 0.42;
  this.vx = (Math.random() - 0.5) * 0.2;
  this.rot = Math.random() * Math.PI * 2;
  this.rv  = (Math.random() - 0.5) * 0.006;
  this.sparkPhase = Math.random() * Math.PI * 2;
  var r = Math.random();
  this.col = r < 0.48 ? 'purple' : (r < 0.78 ? 'white' : 'gold');
  this.op  = 0.25 + Math.random() * 0.5;
}};
Diamond.prototype.update = function() {{
  this.y += this.vy; this.x += this.vx; this.rot += this.rv;
  if (this.y > H + 90) this.reset(false);
}};
Diamond.prototype.draw = function(cx, t) {{
  cx.save();
  cx.translate(this.x, this.y);
  cx.rotate(this.rot);
  cx.globalAlpha = this.op;
  var s = this.s;

  // Brilliant-cut proportions (front/slight-angle view)
  var tw = s * 0.58, ty = -s * 0.50;  // table: half-width, y
  var gw = s * 0.80, gy =  s * 0.08;  // girdle: half-width, y
  var cu = s * 0.96;                    // culet y (bottom tip)
  var mx = gw * 0.44;                   // mid-girdle division x
  var td = tw * 0.40;                   // crown inner division x

  // Per-colour palettes: dk=darkest dm=dark-mid lm=light-mid lt=lightest
  var dk, dm, lm, lt, glowC, edC, glC;
  if (this.col === 'purple') {{
    dk='rgba(12,0,30,0.97)';  dm='rgba(50,0,110,0.93)';
    lm='rgba(130,50,215,0.88)'; lt='rgba(210,155,255,0.92)';
    glowC='rgba(155,48,255,0.9)'; edC='#9B30FF'; glC='#F0DFFF';
  }} else if (this.col === 'white') {{
    // White/clear diamond: near-black interior, ice-blue/white reflections
    dk='rgba(2,4,12,0.98)';   dm='rgba(8,15,38,0.94)';
    lm='rgba(130,185,235,0.88)'; lt='rgba(220,240,255,0.94)';
    glowC='rgba(190,220,255,0.82)'; edC='#B8D8FF'; glC='#FFFFFF';
  }} else {{
    dk='rgba(28,10,0,0.97)';  dm='rgba(90,42,0,0.93)';
    lm='rgba(205,145,8,0.88)'; lt='rgba(255,230,95,0.92)';
    glowC='rgba(255,215,0,0.9)'; edC='#FFD700'; glC='#FFFCE0';
  }}

  // Helpers
  function grd(x0,y0,x1,y1,c0,c1) {{
    var g = cx.createLinearGradient(x0,y0,x1,y1);
    g.addColorStop(0,c0); g.addColorStop(1,c1); return g;
  }}
  function quad(ax,ay,bx,by,px,py,qx,qy,fill) {{
    cx.beginPath(); cx.moveTo(ax,ay); cx.lineTo(bx,by); cx.lineTo(px,py); cx.lineTo(qx,qy);
    cx.closePath(); cx.fillStyle=fill; cx.fill();
  }}
  function tri(ax,ay,bx,by,px,py,fill) {{
    cx.beginPath(); cx.moveTo(ax,ay); cx.lineTo(bx,by); cx.lineTo(px,py);
    cx.closePath(); cx.fillStyle=fill; cx.fill();
  }}

  // ── CROWN: 4 filled quadrilateral facets ──
  // Alternate dark/light to simulate angled planes catching different light
  quad(-tw,ty, -gw,gy, -mx,gy, -td,ty, grd(-gw,gy,-td,ty,dm,dk));   // far-left  (dark)
  quad(-td,ty, -mx,gy,  0,gy,   0,ty,  grd(-mx,gy,0,ty,lm,lt));     // near-left (light)
  quad(  0,ty,   0,gy, mx,gy,  td,ty,  grd(0,gy,td,ty,dm,dk));      // near-right(dark)
  quad( td,ty,  mx,gy, gw,gy,  tw,ty,  grd(mx,gy,tw,ty,lm,lt));     // far-right (light)

  // ── TABLE: reflective flat top ──
  var tg = cx.createLinearGradient(-tw,ty,tw,ty);
  tg.addColorStop(0,lt); tg.addColorStop(0.28,dk); tg.addColorStop(0.55,lt); tg.addColorStop(1,dm);
  quad(-tw,ty, -td,ty, td,ty, tw,ty, tg);  // degenerate line — table is just the top edge,
  // draw it as a thin strip:
  cx.beginPath(); cx.moveTo(-tw,ty); cx.lineTo(tw,ty);
  cx.strokeStyle=glC; cx.lineWidth=2.0; cx.stroke();

  // ── PAVILION: 4 filled triangular facets ──
  tri(-gw,gy, -mx,gy,  0,cu, grd(-gw,gy,0,cu,lm,dk));   // far-left  (light→dark)
  tri(-mx,gy,   0,gy,  0,cu, grd(-mx,gy,0,cu,lt,lm));    // near-left (bright)
  tri(  0,gy,  mx,gy,  0,cu, grd(0,gy,mx,gy,dk,lm));     // near-right(dark→mid)
  tri( mx,gy,  gw,gy,  0,cu, grd(mx,gy,0,cu,dm,lt));     // far-right (mid→light)

  // ── GLOW SILHOUETTE ──
  cx.shadowColor=glowC; cx.shadowBlur=18;
  cx.strokeStyle=edC; cx.lineWidth=1.1;
  cx.beginPath();
  cx.moveTo(-tw,ty); cx.lineTo(tw,ty);
  cx.lineTo(gw,gy); cx.lineTo(0,cu); cx.lineTo(-gw,gy); cx.closePath();
  cx.stroke();
  cx.shadowBlur=0;

  // ── INTERIOR FACET LINES ──
  cx.globalAlpha = this.op * 0.48;
  cx.strokeStyle = edC; cx.lineWidth = 0.55;
  [[-td,ty,-mx,gy],[0,ty,0,gy],[td,ty,mx,gy],[-mx,gy,0,cu],[mx,gy,0,cu]].forEach(function(l) {{
    cx.beginPath(); cx.moveTo(l[0],l[1]); cx.lineTo(l[2],l[3]); cx.stroke();
  }});

  // Girdle bright line
  cx.globalAlpha = this.op;
  cx.strokeStyle = edC; cx.lineWidth = 1.1;
  cx.beginPath(); cx.moveTo(-gw,gy); cx.lineTo(gw,gy); cx.stroke();

  // ── EDGE HIGHLIGHTS (simulate key light) ──
  cx.strokeStyle = glC;
  cx.lineWidth = 1.6;
  cx.beginPath(); cx.moveTo(tw,ty); cx.lineTo(gw,gy); cx.stroke();  // right crown edge
  cx.lineWidth = 1.1;
  cx.beginPath(); cx.moveTo(-gw,gy); cx.lineTo(0,cu); cx.stroke(); // left pavilion edge

  // Culet glint
  cx.lineWidth = 1.5;
  var cg = s*0.04;
  cx.beginPath(); cx.moveTo(-cg,cu); cx.lineTo(cg,cu); cx.stroke();

  // ── 4-POINTED SPARKLE FLASH ──
  var flash = (Math.sin(t*0.003 + this.sparkPhase) + 1) * 0.5;
  if (flash > 0.64) {{
    cx.globalAlpha = this.op * ((flash-0.64)/0.36);
    cx.strokeStyle = glC;
    var ss = s*0.16, spx = tw*0.22, spy = ty + s*0.05;
    cx.lineWidth = 1.1;
    cx.beginPath(); cx.moveTo(spx-ss,spy); cx.lineTo(spx+ss,spy); cx.stroke();
    cx.beginPath(); cx.moveTo(spx,spy-ss*0.75); cx.lineTo(spx,spy+ss*0.75); cx.stroke();
    cx.lineWidth = 0.55;
    cx.beginPath(); cx.moveTo(spx-ss*0.6,spy-ss*0.6); cx.lineTo(spx+ss*0.6,spy+ss*0.6); cx.stroke();
    cx.beginPath(); cx.moveTo(spx+ss*0.6,spy-ss*0.6); cx.lineTo(spx-ss*0.6,spy+ss*0.6); cx.stroke();
  }}

  cx.restore();
}};

var gems = [];
for (var i = 0; i < 22; i++) {{ gems.push(new Diamond(true)); }}

function drawGrid(t) {{
  var cols = 24, rows = 16, cw = W/cols, ch = H/rows;
  c.strokeStyle = 'rgba(61,16,96,0.3)'; c.lineWidth = 0.5;
  for (var r = 0; r <= rows; r++) {{ c.beginPath(); c.moveTo(0,r*ch); c.lineTo(W,r*ch); c.stroke(); }}
  for (var col = 0; col <= cols; col++) {{ c.beginPath(); c.moveTo(col*cw,0); c.lineTo(col*cw,H); c.stroke(); }}
  for (var nr = 2; nr < rows; nr += 4) {{
    for (var nc = 2; nc < cols; nc += 5) {{
      var g = (Math.sin(t*0.0008 + nr*1.3 + nc*0.7) + 1) / 2;
      var useG = (nr + nc) % 3 === 0;
      c.fillStyle = useG ? 'rgba(255,215,0,' + (g*0.32) + ')' : 'rgba(155,48,255,' + (g*0.38) + ')';
      c.beginPath(); c.arc(nc*cw, nr*ch, 1.4 + g*0.6, 0, Math.PI*2); c.fill();
    }}
  }}
}}

var beams = [
  {{ sx:0.22, sp:0.00015, w:110, gold:false }},
  {{ sx:0.62, sp:0.00009, w:150, gold:true  }},
  {{ sx:0.88, sp:0.00022, w:80,  gold:false }},
];
function drawBeams(t) {{
  beams.forEach(function(b, i) {{
    var ox = Math.sin(t*b.sp + i*2.1) * W * 0.11;
    var bx = W*b.sx + ox;
    c.save(); c.translate(bx, 0);
    var g = c.createLinearGradient(0,0,0,H);
    if (b.gold) {{ g.addColorStop(0,'rgba(255,215,0,0.05)'); g.addColorStop(0.45,'rgba(255,215,0,0.02)'); }}
    else {{ g.addColorStop(0,'rgba(155,48,255,0.055)'); g.addColorStop(0.45,'rgba(155,48,255,0.02)'); }}
    g.addColorStop(1,'rgba(0,0,0,0)');
    c.fillStyle = g;
    var bw = b.w;
    c.beginPath(); c.moveTo(-bw*0.25,0); c.lineTo(bw*0.25,0); c.lineTo(bw*1.3,H); c.lineTo(-bw*1.3,H); c.fill();
    c.restore();
  }});
}}

function drawCenterGlow() {{
  var g = c.createRadialGradient(W/2,H*0.4,0,W/2,H*0.4,W*0.55);
  g.addColorStop(0,'rgba(155,48,255,0.045)'); g.addColorStop(0.5,'rgba(155,48,255,0.018)'); g.addColorStop(1,'rgba(0,0,0,0)');
  c.fillStyle = g; c.fillRect(0,0,W,H);
}}

function animate(t) {{
  c.clearRect(0,0,W,H);
  drawCenterGlow(); drawGrid(t); drawBeams(t);
  gems.forEach(function(d) {{ d.update(t); d.draw(c, t); }});
  requestAnimationFrame(animate);
}}
requestAnimationFrame(animate);
</script>

</body>
</html>"""
    return HTMLResponse(content=html)
