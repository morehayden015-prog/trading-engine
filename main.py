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
    ft          = FeeTracker()
    cb          = ft.get_circuit_breaker_status()
    ft.close()
    stats       = executor.get_session_stats()
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
    <div class="agent-status">16 STRATEGIES<br>4 MARKETS</div>
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

// Diamond constructor
function Diamond(spread) {{ this.reset(spread); }}
Diamond.prototype.reset = function(spread) {{
  this.x = Math.random() * W;
  this.y = spread ? Math.random() * H : -60 - Math.random() * 300;
  this.size = 10 + Math.random() * 28;
  this.vy = 0.22 + Math.random() * 0.5;
  this.vx = (Math.random() - 0.5) * 0.25;
  this.rot = Math.random() * Math.PI * 2;
  this.rv = (Math.random() - 0.5) * 0.007;
  var r = Math.random();
  this.col = r < 0.62 ? 'purple' : (r < 0.88 ? 'gold' : 'dark');
  this.op = 0.12 + Math.random() * 0.28;
  this.lw = 0.7 + Math.random() * 1.0;
}};
Diamond.prototype.update = function() {{
  this.y += this.vy; this.x += this.vx; this.rot += this.rv;
  if (this.y > H + 70) this.reset(false);
}};
Diamond.prototype.draw = function(cx) {{
  cx.save();
  cx.translate(this.x, this.y);
  cx.rotate(this.rot);
  cx.globalAlpha = this.op;
  var s = this.size;
  var stroke = this.col === 'purple' ? '#9B30FF' : (this.col === 'gold' ? '#FFD700' : '#3D1060');
  var glow   = this.col === 'purple' ? 'rgba(155,48,255,0.7)' : (this.col === 'gold' ? 'rgba(255,215,0,0.7)' : 'rgba(61,16,96,0.5)');
  var inner  = this.col === 'purple' ? 'rgba(199,125,255,0.35)' : (this.col === 'gold' ? 'rgba(255,215,0,0.3)' : 'rgba(100,50,150,0.2)');
  // outer
  cx.beginPath(); cx.moveTo(0,-s); cx.lineTo(s*0.58,0); cx.lineTo(0,s); cx.lineTo(-s*0.58,0); cx.closePath();
  cx.fillStyle = 'rgba(5,0,8,0.8)'; cx.fill();
  cx.shadowColor = glow; cx.shadowBlur = 10;
  cx.strokeStyle = stroke; cx.lineWidth = this.lw; cx.stroke();
  // inner facet
  cx.beginPath(); cx.moveTo(0,-s*0.48); cx.lineTo(s*0.3,0); cx.lineTo(0,s*0.48); cx.lineTo(-s*0.3,0); cx.closePath();
  cx.shadowBlur = 0; cx.strokeStyle = inner; cx.lineWidth = 0.5; cx.stroke();
  // equator
  cx.beginPath(); cx.moveTo(-s*0.58,0); cx.lineTo(s*0.58,0);
  cx.strokeStyle = this.col==='purple'?'rgba(155,48,255,0.2)':(this.col==='gold'?'rgba(255,215,0,0.2)':'rgba(61,16,96,0.15)');
  cx.lineWidth = 0.4; cx.stroke();
  cx.restore();
}};

var gems = [];
for (var i = 0; i < 20; i++) {{ gems.push(new Diamond(true)); }}

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
  gems.forEach(function(d) {{ d.update(); d.draw(c); }});
  requestAnimationFrame(animate);
}}
requestAnimationFrame(animate);
</script>

</body>
</html>"""
    return HTMLResponse(content=html)
