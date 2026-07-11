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

    regime_clr  = {{"TRENDING_BULL":"#00ffe7","TRENDING_BEAR":"#ff1744","RANGING":"#ffb300","VOLATILE":"#ff6d00","TRANSITIONING":"#d500f9","UNKNOWN":"#546e7a"}}.get(regime,"#546e7a")
    cb_clr      = {{"green":"#00ffe7","yellow":"#ffb300","red":"#ff1744"}}.get(cb["status"],"#546e7a")
    pnl_clr     = "#00ffe7" if total_pnl >= 0 else "#ff1744"
    mkt_clr     = "#00ffe7" if market_open else "#ff1744"
    mkt_txt     = "ONLINE" if market_open else "WEEKEND"

    open_rows = ""
    for t in trades_open:
        d      = t["direction"].upper()
        d_clr  = "#00ffe7" if d in ("BUY","LONG") else "#ff1744"
        d_icon = "▲" if d in ("BUY","LONG") else "▼"
        open_rows += f"""<tr>
          <td class="mono accent">{t['trade_id']}</td>
          <td class="mono">{t['symbol']}</td>
          <td class="mono" style="color:{d_clr}">{d_icon} {d}</td>
          <td class="mono dim">{t['strategy'].replace('_',' ').upper()}</td>
          <td class="mono">{t['entry_price']}</td>
          <td class="mono" style="color:#ffb300">{t['score']:.1f}</td>
          <td class="mono dim">{t['entry_time'][:16].replace('T',' ')}</td>
        </tr>"""

    sym_bias_html = ""
    for sym, b in ctx.get("symbol_bias", {{}}).items():
        bc = "#00ffe7" if b=="BULL" else "#ff1744" if b=="BEAR" else "#546e7a"
        sym_bias_html += f'<div class="sym-node"><span class="sym-name">{sym}</span><span class="sym-val" style="color:{bc}">{b}</span></div>'

    now_str = datetime.utcnow().strftime("%Y.%m.%d  %H:%M:%S UTC")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>NEXUS // TRADING ENGINE</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&display=swap');

:root {{
  --cyan:   #00ffe7;
  --gold:   #ffb300;
  --red:    #ff1744;
  --purple: #d500f9;
  --bg:     #020b10;
  --bg2:    #040f17;
  --bg3:    #061520;
  --border: #0a3040;
  --dim:    #2a4a5a;
  --text:   #8ab8c8;
}}

* {{ box-sizing:border-box; margin:0; padding:0; }}

body {{
  background: var(--bg);
  color: var(--text);
  font-family: 'Share Tech Mono', 'Courier New', monospace;
  min-height: 100vh;
  overflow-x: hidden;
}}

/* scanline overlay */
body::after {{
  content:'';
  position:fixed; top:0; left:0; right:0; bottom:0;
  background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,255,231,0.015) 2px, rgba(0,255,231,0.015) 4px);
  pointer-events:none; z-index:9999;
}}

.wrap {{ max-width:1400px; margin:0 auto; padding:24px 20px; }}

/* ── HEADER ── */
header {{
  display:flex; align-items:center; justify-content:space-between;
  border-bottom:1px solid var(--border);
  padding-bottom:16px; margin-bottom:28px;
}}
.logo {{ display:flex; align-items:center; gap:14px; }}
.logo-mark {{
  width:44px; height:44px;
  border:2px solid var(--cyan);
  transform:rotate(45deg);
  display:flex; align-items:center; justify-content:center;
  box-shadow: 0 0 16px rgba(0,255,231,.3);
}}
.logo-mark-inner {{
  width:18px; height:18px;
  background: var(--cyan);
  transform:rotate(0deg);
  opacity:.9;
}}
.logo-text {{ line-height:1.2; }}
.logo-title {{
  font-family:'Orbitron',monospace;
  font-size:1.3rem; font-weight:900;
  color:#fff;
  letter-spacing:4px;
  text-shadow: 0 0 20px rgba(0,255,231,.5);
}}
.logo-sub {{
  font-size:.65rem; color:var(--cyan); letter-spacing:6px; opacity:.7;
}}
.header-right {{ text-align:right; }}
.clock {{ font-size:.75rem; color:var(--cyan); letter-spacing:2px; opacity:.8; }}
.mode-badge {{
  display:inline-block; margin-top:4px;
  font-size:.6rem; letter-spacing:3px;
  border:1px solid var(--gold); color:var(--gold);
  padding:2px 8px;
}}

/* ── SECTION LABEL ── */
.section-label {{
  font-family:'Orbitron',monospace;
  font-size:.6rem; letter-spacing:4px;
  color:var(--cyan); opacity:.6;
  margin-bottom:12px;
  display:flex; align-items:center; gap:10px;
}}
.section-label::after {{
  content:''; flex:1; height:1px; background:var(--border);
}}

/* ── CARDS GRID ── */
.grid {{
  display:grid;
  grid-template-columns: repeat(auto-fit, minmax(155px,1fr));
  gap:10px; margin-bottom:28px;
}}

.card {{
  background: var(--bg2);
  border:1px solid var(--border);
  padding:14px 16px;
  position:relative;
  clip-path: polygon(0 0, calc(100% - 12px) 0, 100% 12px, 100% 100%, 0 100%);
}}
.card::before {{
  content:'';
  position:absolute; top:0; left:0; right:0; height:1px;
  background: linear-gradient(90deg, var(--cyan), transparent);
  opacity:.4;
}}
.card-label {{
  font-size:.6rem; letter-spacing:3px;
  color:var(--dim); text-transform:uppercase;
  margin-bottom:8px;
}}
.card-value {{
  font-family:'Orbitron',monospace;
  font-size:1.3rem; font-weight:700;
  line-height:1;
}}
.card-sub {{
  font-size:.65rem; color:var(--dim);
  margin-top:6px; letter-spacing:1px;
}}

/* ── SYMBOL BIAS ROW ── */
.sym-row {{
  display:flex; gap:8px; flex-wrap:wrap;
  margin-bottom:28px;
}}
.sym-node {{
  background:var(--bg2);
  border:1px solid var(--border);
  padding:8px 14px;
  display:flex; flex-direction:column; align-items:center; gap:3px;
  clip-path: polygon(8px 0, 100% 0, 100% calc(100% - 8px), calc(100% - 8px) 100%, 0 100%, 0 8px);
  min-width:80px;
}}
.sym-name {{ font-size:.6rem; letter-spacing:3px; color:var(--dim); }}
.sym-val  {{ font-family:'Orbitron',monospace; font-size:.75rem; font-weight:700; letter-spacing:2px; }}

/* ── TABLE ── */
.table-wrap {{
  background:var(--bg2);
  border:1px solid var(--border);
  overflow:hidden;
  margin-bottom:28px;
  clip-path: polygon(0 0, calc(100% - 16px) 0, 100% 16px, 100% 100%, 0 100%);
}}
table {{ width:100%; border-collapse:collapse; }}
th {{
  font-size:.58rem; letter-spacing:3px; text-transform:uppercase;
  color:var(--dim); padding:10px 14px; text-align:left;
  border-bottom:1px solid var(--border);
  background:var(--bg3);
}}
td {{ padding:10px 14px; border-bottom:1px solid rgba(10,48,64,.5); }}
tr:last-child td {{ border-bottom:none; }}
tr:hover td {{ background:rgba(0,255,231,.03); }}
.mono {{ font-family:'Share Tech Mono',monospace; font-size:.82rem; }}
.accent {{ color:var(--cyan); }}
.dim {{ color:var(--dim); }}

/* ── AGENT STATUS ── */
.agents {{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
  gap:8px; margin-bottom:28px;
}}
.agent-card {{
  background:var(--bg2);
  border:1px solid var(--border);
  border-left:2px solid var(--cyan);
  padding:10px 14px;
  display:flex; align-items:center; gap:10px;
}}
.agent-dot {{
  width:6px; height:6px; border-radius:50%;
  background:var(--cyan);
  box-shadow: 0 0 8px var(--cyan);
  animation: pulse 2s infinite;
  flex-shrink:0;
}}
@keyframes pulse {{
  0%,100% {{ opacity:1; }}
  50% {{ opacity:.3; }}
}}
.agent-name {{ font-size:.65rem; letter-spacing:2px; color:var(--text); }}
.agent-status {{ font-size:.58rem; color:var(--dim); margin-top:2px; }}

/* ── FOOTER ── */
footer {{
  border-top:1px solid var(--border);
  padding-top:12px;
  display:flex; justify-content:space-between; align-items:center;
}}
.footer-txt {{ font-size:.6rem; color:var(--dim); letter-spacing:2px; }}
.live-dot {{
  display:inline-block; width:6px; height:6px; border-radius:50%;
  background:var(--cyan); margin-right:6px;
  box-shadow:0 0 8px var(--cyan);
  animation:pulse 1.5s infinite;
  vertical-align:middle;
}}

/* glow helpers */
.glow-cyan  {{ text-shadow:0 0 12px rgba(0,255,231,.6); }}
.glow-gold  {{ text-shadow:0 0 12px rgba(255,179,0,.6); }}
.glow-red   {{ text-shadow:0 0 12px rgba(255,23,68,.6); }}
</style>
</head>
<body>
<div class="wrap">

<!-- HEADER -->
<header>
  <div class="logo">
    <div class="logo-mark"><div class="logo-mark-inner"></div></div>
    <div class="logo-text">
      <div class="logo-title">NEXUS</div>
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
    <div class="card-value glow-cyan" style="color:var(--cyan);font-size:1rem">● {mkt_txt}</div>
    <div class="card-sub" style="color:{mkt_clr}">MARKETS {"OPEN" if market_open else "CLOSED"}</div>
  </div>

  <div class="card">
    <div class="card-label">Circuit Breaker</div>
    <div class="card-value" style="color:{cb_clr}">{cb['status'].upper()}</div>
    <div class="card-sub">{cb['reason'][:35]}</div>
  </div>

  <div class="card">
    <div class="card-label">Regime</div>
    <div class="card-value" style="color:{regime_clr};font-size:.85rem">{regime.replace('_',' ')}</div>
    <div class="card-sub">BIAS: {bias}</div>
  </div>

  <div class="card">
    <div class="card-label">Open Positions</div>
    <div class="card-value glow-cyan" style="color:var(--cyan)">{len(trades_open)}</div>
    <div class="card-sub">MAX CONCURRENT: {MAX_CONCURRENT}</div>
  </div>

  <div class="card">
    <div class="card-label">Win Rate</div>
    <div class="card-value glow-gold" style="color:var(--gold)">{win_rate}</div>
    <div class="card-sub">{wins}W · {losses}L · {total_trades} TOTAL</div>
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
    <div class="card-value" style="color:var(--gold)">{fear_greed}</div>
    <div class="card-sub">{fg_label.upper()}</div>
  </div>

  <div class="card">
    <div class="card-label">DXY Trend</div>
    <div class="card-value" style="color:var(--text);font-size:.9rem">{dxy}</div>
    <div class="card-sub">DOLLAR STRENGTH</div>
  </div>

  <div class="card">
    <div class="card-label">Size Multiplier</div>
    <div class="card-value glow-gold" style="color:var(--gold)">{sizing}×</div>
    <div class="card-sub">DYNAMIC RISK SCALE</div>
  </div>

</div>

<!-- AGENT STATUS -->
<div class="section-label">ACTIVE AGENTS</div>
<div class="agents">
  <div class="agent-card"><div class="agent-dot"></div><div><div class="agent-name">INTEL AGENT</div><div class="agent-status">VIX · DXY · FEAR&amp;GREED // 15 MIN</div></div></div>
  <div class="agent-card"><div class="agent-dot"></div><div><div class="agent-name">REGIME AGENT</div><div class="agent-status">CLAUDE + WEB SEARCH // 30 MIN</div></div></div>
  <div class="agent-card"><div class="agent-dot"></div><div><div class="agent-name">RISK AGENT</div><div class="agent-status">DRAWDOWN · SIZING // 60 SEC</div></div></div>
  <div class="agent-card"><div class="agent-dot"></div><div><div class="agent-name">TRADE MONITOR</div><div class="agent-status">BE · PARTIAL · TP/SL // 30 SEC</div></div></div>
  <div class="agent-card"><div class="agent-dot"></div><div><div class="agent-name">SCANNER</div><div class="agent-status">16 STRATEGIES · 4 MARKETS // 5 MIN</div></div></div>
  <div class="agent-card"><div class="agent-dot"></div><div><div class="agent-name">CALIBRATION</div><div class="agent-status">SELF-LEARNING // 25 TRADES</div></div></div>
  <div class="agent-card"><div class="agent-dot"></div><div><div class="agent-name">BRIEFING</div><div class="agent-status">DAILY MARKET INTEL // 08:00 EST</div></div></div>
</div>

<!-- SYMBOL BIAS -->
<div class="section-label">SYMBOL INTELLIGENCE</div>
<div class="sym-row" style="margin-bottom:28px">
{sym_bias_html if sym_bias_html else '<span style="color:var(--dim);font-size:.75rem">AWAITING REGIME CLASSIFICATION...</span>'}
</div>

<!-- OPEN POSITIONS -->
<div class="section-label">OPEN POSITIONS</div>
{'<div style="color:var(--dim);font-size:.75rem;letter-spacing:2px;padding:16px 0">NO ACTIVE POSITIONS</div>' if not trades_open else f'''<div class="table-wrap"><table>
  <thead><tr>
    <th>TRADE ID</th><th>SYMBOL</th><th>DIRECTION</th><th>STRATEGY</th><th>ENTRY</th><th>SCORE</th><th>OPENED</th>
  </tr></thead>
  <tbody>{open_rows}</tbody>
</table></div>'''}

<!-- WEEKEND COUNTDOWN (shown only when market closed) -->
{"" if market_open else '''
<div class="section-label">MARKETS REOPEN IN</div>
<div style="background:var(--bg2);border:1px solid var(--border);border-left:2px solid var(--gold);padding:16px 20px;margin-bottom:28px;display:flex;align-items:center;gap:16px">
  <div style="font-family:Orbitron,monospace;font-size:2rem;font-weight:900;color:var(--gold);text-shadow:0 0 20px rgba(255,179,0,.5)" id="countdown">--:--:--</div>
  <div style="font-size:.65rem;letter-spacing:2px;color:var(--dim)">UNTIL SUNDAY 23:00 UTC<br>ALL AGENTS STANDING BY</div>
</div>
<script>
function getSecondsUntilOpen() {
  const now = new Date();
  const day = now.getUTCDay(); // 0=Sun, 6=Sat
  const h = now.getUTCHours(), m = now.getUTCMinutes(), s = now.getUTCSeconds();
  let target = new Date(now);
  if (day === 6) {
    // Saturday → target is next Sunday 23:00 UTC
    target.setUTCDate(target.getUTCDate() + 1);
  } else {
    // Sunday before 23:00
    target = new Date(target);
  }
  target.setUTCHours(23, 0, 0, 0);
  return Math.max(0, Math.floor((target - now) / 1000));
}
function fmt(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  return [h,m,sec].map(x=>String(x).padStart(2,'0')).join(':');
}
function tick() {
  const el = document.getElementById('countdown');
  if (el) el.textContent = fmt(getSecondsUntilOpen());
}
tick(); setInterval(tick, 1000);
</script>
'''}

<!-- FOOTER -->
<footer>
  <div class="footer-txt"><span class="live-dot"></span>LIVE DATA · AUTO-REFRESH 30S</div>
  <div class="footer-txt">NEXUS ENGINE v2.0 // {now_str}</div>
</footer>

</div>
</body>
</html>"""
    return HTMLResponse(content=html)
