"""
scanner.py
----------
Active market scanner — runs every 5 minutes, detects all strategy
setups across all markets using live OHLCV data from yfinance.

Fires signals directly into main.py's webhook pipeline so every
signal is scored, AI-evaluated, and paper-executed the same way
TradingView webhooks are.

Markets scanned: XAUUSD, ES, NQ, CL
Timeframes:      5m (entry), 15m (structure), 1h (bias)
Strategies:      15 total (6 original + 9 new)

Run standalone:  python scanner.py
Or imported:     from scanner import start_scanner (called by main.py)
"""

import asyncio
import logging
import math
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
import yfinance as yf
import pandas as pd
import numpy as np

from self_learning import is_strategy_enabled

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

WEBHOOK_URL    = "http://localhost:8000/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "hayden_private_key")
SCAN_INTERVAL  = 300

SYMBOL_MAP = {
    "XAUUSD": "GC=F",
    "ES":     "ES=F",
    "NQ":     "NQ=F",
    "CL":     "CL=F",
}

STRATEGY_MARKETS = {
    "sweep_bos_fvg":   ["XAUUSD"],
    "rp_profits":      ["ES", "NQ", "XAUUSD"],
    "ict_5step":       ["NQ", "ES", "XAUUSD"],
    "orb_scalp":       ["ES", "NQ", "CL"],
    "supply_demand":   ["XAUUSD", "ES", "NQ", "CL"],
    "mamba_scalp":     ["NQ", "ES"],
    "turtle_soup":     ["XAUUSD", "ES", "NQ"],
    "silver_bullet":   ["XAUUSD", "NQ", "ES"],
    "judas_swing":     ["XAUUSD", "NQ", "ES"],
    "engulfing":       ["XAUUSD", "ES", "NQ", "CL"],
    "pin_bar":         ["XAUUSD", "ES", "NQ", "CL"],
    "inside_bar":      ["XAUUSD", "ES", "NQ", "CL"],
    "morning_star":    ["XAUUSD", "ES", "NQ"],
    "vwap_reclaim":    ["ES", "NQ", "CL"],
    "ema_cross":       ["XAUUSD", "ES", "NQ", "CL"],
    "rsi_divergence":  ["XAUUSD", "ES", "NQ"],
}

# ── Data fetcher ──────────────────────────────────────────────────────────────

def fetch_ohlcv(symbol: str, interval: str, period: str = "5d"):
    try:
        yf_symbol = SYMBOL_MAP.get(symbol, symbol)
        df = yf.download(yf_symbol, period=period, interval=interval, progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, 'levels'):
            df.columns = [col[0].lower() for col in df.columns]
        else:
            df.columns = [col.lower() for col in df.columns]
        return df
    except Exception as e:
        logger.error(f"fetch_ohlcv error {symbol} {interval}: {e}")
        return None

# ── Indicators ────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift()).abs()
    lc  = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def vwap(df: pd.DataFrame) -> pd.Series:
    df = df.copy()
    df["date"]    = pd.to_datetime(df.index).normalize()
    df["tp"]      = (df["high"] + df["low"] + df["close"]) / 3
    df["tp_vol"]  = df["tp"] * df["volume"]
    df["cum_tpv"] = df.groupby("date")["tp_vol"].cumsum()
    df["cum_vol"] = df.groupby("date")["volume"].cumsum()
    return df["cum_tpv"] / df["cum_vol"]

def pivot_highs(series: pd.Series, left: int = 2, right: int = 2) -> pd.Series:
    result = pd.Series(False, index=series.index)
    for i in range(left, len(series) - right):
        window = series.iloc[i - left: i + right + 1]
        if series.iloc[i] == window.max():
            result.iloc[i] = True
    return result

def pivot_lows(series: pd.Series, left: int = 2, right: int = 2) -> pd.Series:
    result = pd.Series(False, index=series.index)
    for i in range(left, len(series) - right):
        window = series.iloc[i - left: i + right + 1]
        if series.iloc[i] == window.min():
            result.iloc[i] = True
    return result

def swing_highs_lows(df: pd.DataFrame, lookback: int = 10):
    recent = df.tail(lookback * 3)
    ph = pivot_highs(recent["high"])
    pl = pivot_lows(recent["low"])
    swing_high = recent["high"][ph].iloc[-1] if ph.any() else df["high"].tail(lookback).max()
    swing_low  = recent["low"][pl].iloc[-1]  if pl.any() else df["low"].tail(lookback).min()
    return swing_high, swing_low

def current_session() -> str:
    hour = datetime.now(timezone.utc).hour
    if 0  <= hour <  7:  return "asian"
    if 7  <= hour < 12:  return "london"
    if 12 <= hour < 17:  return "new_york"
    return "after_hours"

def is_killzone() -> bool:
    hour   = datetime.now(timezone.utc).hour
    minute = datetime.now(timezone.utc).minute
    t = hour * 60 + minute
    return (2*60 <= t <= 5*60) or (13*60 <= t <= 16*60)

def market_open_minutes() -> int:
    now = datetime.now(timezone.utc)
    open_time = now.replace(hour=14, minute=30, second=0, microsecond=0)
    if now < open_time:
        return -1
    return int((now - open_time).total_seconds() / 60)

# ── Strategy detectors ────────────────────────────────────────────────────────

def detect_sweep_bos_fvg(df5, df15, df1h):
    if len(df5) < 30 or len(df15) < 20:
        return None, ""
    c  = df5.iloc[-1]; c1 = df5.iloc[-2]; c2 = df5.iloc[-3]
    bull_fvg = c2["high"] < c["low"]
    bear_fvg = c2["low"]  > c["high"]
    sh, sl = swing_highs_lows(df5, lookback=15)
    bos_bull = c1["close"] > sh and c["close"] > sh
    bos_bear = c1["close"] < sl and c["close"] < sl
    sweep_bull = c1["low"] < sl and c1["close"] > sl
    sweep_bear = c1["high"] > sh and c1["close"] < sh
    if sweep_bull and bos_bull and bull_fvg and is_killzone():
        return "buy", "sweep_bos_fvg: liq sweep + BOS + bullish FVG in killzone"
    if sweep_bear and bos_bear and bear_fvg and is_killzone():
        return "sell", "sweep_bos_fvg: liq sweep + BOS + bearish FVG in killzone"
    return None, ""

def detect_supply_demand(df5, df15, df1h):
    if len(df15) < 40:
        return None, ""
    c = df15.iloc[-1]; atr_ = atr(df15).iloc[-1]
    body_sizes = (df15["close"] - df15["open"]).abs()
    impulse = body_sizes > (atr_ * 1.5)
    demand_zones = df15[impulse & (df15["close"] > df15["open"])].tail(5)
    for _, zone in demand_zones.iterrows():
        if zone["low"] <= c["low"] <= zone["open"] and c["close"] > c["open"]:
            return "buy", f"supply_demand: demand zone retest at {zone['low']:.2f}-{zone['open']:.2f}"
    supply_zones = df15[impulse & (df15["close"] < df15["open"])].tail(5)
    for _, zone in supply_zones.iterrows():
        if zone["open"] <= c["high"] <= zone["high"] and c["close"] < c["open"]:
            return "sell", f"supply_demand: supply zone retest at {zone['open']:.2f}-{zone['high']:.2f}"
    return None, ""

def detect_orb_scalp(df5, df15, df1h):
    mins_open = market_open_minutes()
    if mins_open < 0 or mins_open > 90 or len(df15) < 5:
        return None, ""
    orb_high = df15["high"].iloc[-3:-1].max()
    orb_low  = df15["low"].iloc[-3:-1].min()
    c = df15.iloc[-1]; avg_vol = df15["volume"].tail(20).mean()
    if c["close"] > orb_high and c["volume"] > avg_vol * 1.2:
        return "buy", f"orb_scalp: breakout above ORB {orb_high:.2f} with high volume"
    if c["close"] < orb_low and c["volume"] > avg_vol * 1.2:
        return "sell", f"orb_scalp: breakdown below ORB {orb_low:.2f} with high volume"
    return None, ""

def detect_ict_5step(df5, df15, df1h):
    if len(df5) < 50 or not is_killzone():
        return None, ""
    c = df5.iloc[-1]; c1 = df5.iloc[-2]; c2 = df5.iloc[-3]
    atr_ = atr(df5).iloc[-1]
    sh, sl = swing_highs_lows(df5, lookback=20)
    mss_bull = c1["close"] > sh and (c1["close"] - c1["open"]) > atr_ * 0.5
    mss_bear = c1["close"] < sl and (c1["open"] - c1["close"]) > atr_ * 0.5
    fvg_bull = c2["high"] < c["low"]
    fvg_bear = c2["low"]  > c["high"]
    if mss_bull and fvg_bull:
        return "buy", "ict_5step: MSS + OB + FVG bullish confluence"
    if mss_bear and fvg_bear:
        return "sell", "ict_5step: MSS + OB + FVG bearish confluence"
    return None, ""

def detect_mamba_scalp(df5, df15, df1h):
    mins_open = market_open_minutes()
    if mins_open < 0 or mins_open > 60 or len(df5) < 30:
        return None, ""
    c = df5.iloc[-1]; e8 = ema(df5["close"], 8); e21 = ema(df5["close"], 21)
    rsi_ = rsi(df5["close"]).iloc[-1]
    bull_stack = e8.iloc[-1] > e21.iloc[-1] and e8.iloc[-2] > e21.iloc[-2]
    bear_stack = e8.iloc[-1] < e21.iloc[-1] and e8.iloc[-2] < e21.iloc[-2]
    if bull_stack and c["close"] > c["open"] and rsi_ > 55:
        return "buy", f"mamba_scalp: EMA8>21 stack + momentum burst, RSI {rsi_:.1f}"
    if bear_stack and c["close"] < c["open"] and rsi_ < 45:
        return "sell", f"mamba_scalp: EMA8<21 stack + momentum burst, RSI {rsi_:.1f}"
    return None, ""

def detect_rp_profits(df5, df15, df1h):
    now = datetime.now(timezone.utc)
    hour, minute = now.hour, now.minute
    in_rp_window = (hour == 13 and 0 <= minute <= 30) or (hour == 14 and minute <= 30)
    if not in_rp_window or len(df5) < 20:
        return None, ""
    c = df5.iloc[-1]; session_h = df5["high"].tail(20).max(); session_l = df5["low"].tail(20).min()
    c_range = session_h - session_l; atr_ = atr(df5).iloc[-1]
    near_low  = c["low"]  <= session_l + (c_range * 0.15)
    near_high = c["high"] >= session_h - (c_range * 0.15)
    if near_low  and c["close"] > c["open"] and (c["close"] - c["low"])  > atr_ * 0.3:
        return "buy",  f"rp_profits: untested session low {session_l:.2f} reversal"
    if near_high and c["close"] < c["open"] and (c["high"] - c["close"]) > atr_ * 0.3:
        return "sell", f"rp_profits: untested session high {session_h:.2f} reversal"
    return None, ""

def detect_turtle_soup(df5, df15, df1h):
    if len(df5) < 30:
        return None, ""
    c = df5.iloc[-1]; c1 = df5.iloc[-2]; atr_ = atr(df5).iloc[-1]
    prior = df5.iloc[-25:-3]; prior_high = prior["high"].max(); prior_low = prior["low"].min()
    bear = c1["high"] > prior_high and c1["close"] < prior_high and c["close"] < c["open"] and (c1["high"] - prior_high) < atr_ * 0.5
    bull = c1["low"] < prior_low and c1["close"] > prior_low and c["close"] > c["open"] and (prior_low - c1["low"]) < atr_ * 0.5
    if bull:  return "buy",  f"turtle_soup: false break below {prior_low:.2f}"
    if bear:  return "sell", f"turtle_soup: false break above {prior_high:.2f}"
    return None, ""

def detect_silver_bullet(df5, df15, df1h):
    hour = datetime.now(timezone.utc).hour; minute = datetime.now(timezone.utc).minute
    t = hour * 60 + minute
    if not ((3*60 <= t <= 4*60) or (15*60 <= t <= 16*60)) or len(df5) < 20:
        return None, ""
    c = df5.iloc[-1]; c1 = df5.iloc[-2]; c2 = df5.iloc[-3]
    bull_fvg_top = c2["high"]; bull_fvg_bot = c["low"]
    in_bull_fvg = c2["high"] < c1["low"] and bull_fvg_bot <= c["close"] <= bull_fvg_top
    bear_fvg_top = c["high"]; bear_fvg_bot = c2["low"]
    in_bear_fvg = c2["low"] > c1["high"] and bear_fvg_bot <= c["close"] <= bear_fvg_top
    if in_bull_fvg and c["close"] > c["open"]:  return "buy",  "silver_bullet: FVG fill entry during silver bullet window"
    if in_bear_fvg and c["close"] < c["open"]:  return "sell", "silver_bullet: bearish FVG fill during silver bullet window"
    return None, ""

def detect_judas_swing(df5, df15, df1h):
    hour = datetime.now(timezone.utc).hour
    if not (3 <= hour <= 8) or len(df5) < 40 or len(df15) < 10:
        return None, ""
    london_bars = df5.tail(30); session_high = london_bars["high"].max(); session_low = london_bars["low"].min()
    c = df5.iloc[-1]; atr_ = atr(df5).iloc[-1]
    high_early  = df5.iloc[-25:-15]["high"].max(); late_decline = df5.iloc[-10:]["close"].iloc[-1] < df5.iloc[-10:]["close"].iloc[0]
    low_early   = df5.iloc[-25:-15]["low"].min();  late_rally   = df5.iloc[-10:]["close"].iloc[-1] > df5.iloc[-10:]["close"].iloc[0]
    if high_early > session_low + (session_high - session_low) * 0.7 and late_decline and c["close"] < c["open"] and (c["open"] - c["close"]) > atr_ * 0.4:
        return "sell", "judas_swing: early London high swept, reversing south"
    if low_early < session_high - (session_high - session_low) * 0.7 and late_rally and c["close"] > c["open"] and (c["close"] - c["open"]) > atr_ * 0.4:
        return "buy",  "judas_swing: early London low swept, reversing north"
    return None, ""

def detect_engulfing(df5, df15, df1h):
    if len(df5) < 20:
        return None, ""
    c = df5.iloc[-1]; c1 = df5.iloc[-2]; e21 = ema(df5["close"], 21).iloc[-1]
    sh, sl = swing_highs_lows(df5, lookback=15); atr_ = atr(df5).iloc[-1]
    bull_engulf = c["close"] > c["open"] and c["open"] < c1["close"] and c["close"] > c1["open"] and (c["close"] - c["open"]) > (c1["open"] - c1["close"]) * 0.9
    bear_engulf = c["close"] < c["open"] and c["open"] > c1["close"] and c["close"] < c1["open"] and (c["open"] - c["close"]) > (c1["close"] - c1["open"]) * 0.9
    near_sl = abs(c["low"] - sl) < atr_ * 0.5; near_sh = abs(c["high"] - sh) < atr_ * 0.5; near_ema = abs(c["close"] - e21) < atr_ * 0.3
    if bull_engulf and (near_sl or near_ema):  return "buy",  f"engulfing: bullish engulf at key level"
    if bear_engulf and (near_sh or near_ema):  return "sell", f"engulfing: bearish engulf at key level"
    return None, ""

def detect_pin_bar(df5, df15, df1h):
    if len(df15) < 20:
        return None, ""
    c = df15.iloc[-1]; atr_ = atr(df15).iloc[-1]; sh, sl = swing_highs_lows(df15, lookback=20)
    body = abs(c["close"] - c["open"]); upper_wick = c["high"] - max(c["open"], c["close"]); lower_wick = min(c["open"], c["close"]) - c["low"]
    total_range = c["high"] - c["low"]
    has_long_lower = lower_wick > body * 2 and lower_wick > upper_wick * 2
    has_long_upper = upper_wick > body * 2 and upper_wick > lower_wick * 2
    significant = total_range > atr_ * 0.5
    near_sl = abs(c["low"] - sl) < atr_ * 0.6; near_sh = abs(c["high"] - sh) < atr_ * 0.6
    if has_long_lower and near_sl and significant:  return "buy",  f"pin_bar: hammer at support {sl:.2f}"
    if has_long_upper and near_sh and significant:  return "sell", f"pin_bar: shooting star at resistance {sh:.2f}"
    return None, ""

def detect_inside_bar(df5, df15, df1h):
    if len(df15) < 20:
        return None, ""
    c = df15.iloc[-1]; c1 = df15.iloc[-2]; c2 = df15.iloc[-3]
    is_inside = c1["high"] <= c2["high"] and c1["low"] >= c2["low"]
    if not is_inside:
        return None, ""
    bull_break = c["close"] > c2["high"] and c["close"] > c["open"]
    bear_break = c["close"] < c2["low"]  and c["close"] < c["open"]
    e50_1h = ema(df1h["close"], 50).iloc[-1] if len(df1h) > 50 else None
    trend_bull = df1h["close"].iloc[-1] > e50_1h if e50_1h else True
    trend_bear = df1h["close"].iloc[-1] < e50_1h if e50_1h else True
    if bull_break and trend_bull:  return "buy",  f"inside_bar: IB breakout above {c2['high']:.2f}"
    if bear_break and trend_bear:  return "sell", f"inside_bar: IB breakdown below {c2['low']:.2f}"
    return None, ""

def detect_morning_star(df5, df15, df1h):
    if len(df15) < 20:
        return None, ""
    c = df15.iloc[-1]; c1 = df15.iloc[-2]; c2 = df15.iloc[-3]; atr_ = atr(df15).iloc[-1]
    sh, sl = swing_highs_lows(df15, lookback=15)
    first_bear = c2["close"] < c2["open"] and (c2["open"] - c2["close"]) > atr_ * 0.5
    star_small = abs(c1["close"] - c1["open"]) < atr_ * 0.25
    confirm_bull = c["close"] > c["open"] and (c["close"] - c["open"]) > atr_ * 0.4
    at_support = c2["low"] <= sl + atr_ * 0.5
    first_bull = c2["close"] > c2["open"] and (c2["close"] - c2["open"]) > atr_ * 0.5
    confirm_bear = c["close"] < c["open"] and (c["open"] - c["close"]) > atr_ * 0.4
    at_resist = c2["high"] >= sh - atr_ * 0.5
    if first_bear and star_small and confirm_bull and at_support:  return "buy",  f"morning_star: reversal at support {sl:.2f}"
    if first_bull and star_small and confirm_bear and at_resist:   return "sell", f"evening_star: reversal at resistance {sh:.2f}"
    return None, ""

def detect_vwap_reclaim(df5, df15, df1h):
    mins_open = market_open_minutes()
    if mins_open < 0 or mins_open > 360 or len(df5) < 40:
        return None, ""
    try:
        vwap_series = vwap(df5)
    except Exception:
        return None, ""
    c = df5.iloc[-1]; c1 = df5.iloc[-2]; v = vwap_series.iloc[-1]; v1 = vwap_series.iloc[-2]
    avg_vol = df5["volume"].tail(20).mean()
    bull_reclaim = c1["close"] < v1 and c["close"] > v and c["close"] > c["open"] and c["volume"] > avg_vol * 1.1
    bear_lose    = c1["close"] > v1 and c["close"] < v and c["close"] < c["open"] and c["volume"] > avg_vol * 1.1
    if bull_reclaim:  return "buy",  f"vwap_reclaim: price reclaimed VWAP {v:.2f}"
    if bear_lose:     return "sell", f"vwap_reclaim: price lost VWAP {v:.2f}"
    return None, ""

def detect_ema_cross(df5, df15, df1h):
    if len(df5) < 30:
        return None, ""
    e9 = ema(df5["close"], 9); e21 = ema(df5["close"], 21); r = rsi(df5["close"]); c = df5.iloc[-1]
    cross_bull = e9.iloc[-2] <= e21.iloc[-2] and e9.iloc[-1] > e21.iloc[-1]
    cross_bear = e9.iloc[-2] >= e21.iloc[-2] and e9.iloc[-1] < e21.iloc[-1]
    rsi_now = r.iloc[-1]
    if cross_bull and rsi_now > 50 and c["close"] > e21.iloc[-1]:  return "buy",  f"ema_cross: EMA9 crossed above EMA21, RSI {rsi_now:.0f}"
    if cross_bear and rsi_now < 50 and c["close"] < e21.iloc[-1]:  return "sell", f"ema_cross: EMA9 crossed below EMA21, RSI {rsi_now:.0f}"
    return None, ""

def detect_rsi_divergence(df5, df15, df1h):
    if len(df15) < 40:
        return None, ""
    closes = df15["close"]; highs = df15["high"]; lows = df15["low"]; rsi_ = rsi(closes)
    window = 20
    recent_close = closes.tail(window); recent_high = highs.tail(window); recent_low = lows.tail(window); recent_rsi = rsi_.tail(window)
    price_ll = recent_low.iloc[-1]  < recent_low.iloc[:window//2].min()
    rsi_hl   = recent_rsi.iloc[-1]  > recent_rsi.iloc[:window//2].min()
    rsi_os   = recent_rsi.iloc[-1]  < 40
    price_hh = recent_high.iloc[-1] > recent_high.iloc[:window//2].max()
    rsi_lh   = recent_rsi.iloc[-1]  < recent_rsi.iloc[:window//2].max()
    rsi_ob   = recent_rsi.iloc[-1]  > 60
    c = df15.iloc[-1]
    if price_ll and rsi_hl and rsi_os and c["close"] > c["open"]:  return "buy",  f"rsi_divergence: bullish divergence, RSI {recent_rsi.iloc[-1]:.0f}"
    if price_hh and rsi_lh and rsi_ob and c["close"] < c["open"]:  return "sell", f"rsi_divergence: bearish divergence, RSI {recent_rsi.iloc[-1]:.0f}"
    return None, ""

# ── Strategy registry ─────────────────────────────────────────────────────────

STRATEGY_DETECTORS = {
    "sweep_bos_fvg":  detect_sweep_bos_fvg,
    "rp_profits":     detect_rp_profits,
    "ict_5step":      detect_ict_5step,
    "orb_scalp":      detect_orb_scalp,
    "supply_demand":  detect_supply_demand,
    "mamba_scalp":    detect_mamba_scalp,
    "turtle_soup":    detect_turtle_soup,
    "silver_bullet":  detect_silver_bullet,
    "judas_swing":    detect_judas_swing,
    "engulfing":      detect_engulfing,
    "pin_bar":        detect_pin_bar,
    "inside_bar":     detect_inside_bar,
    "morning_star":   detect_morning_star,
    "vwap_reclaim":   detect_vwap_reclaim,
    "ema_cross":      detect_ema_cross,
    "rsi_divergence": detect_rsi_divergence,
}

# ── Signal deduplication ──────────────────────────────────────────────────────

_fired_signals: dict = {}

def _is_duplicate(symbol: str, strategy: str, cooldown_min: int = 30) -> bool:
    key = f"{symbol}_{strategy}"
    last = _fired_signals.get(key)
    if last is None:
        return False
    return (time.time() - last) < (cooldown_min * 60)

def _mark_fired(symbol: str, strategy: str):
    _fired_signals[f"{symbol}_{strategy}"] = time.time()

# ── Main scan loop ────────────────────────────────────────────────────────────

async def run_single_scan():
    print(f"\n[scanner] ── scan started {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} ──")
    signals_fired = 0

    for symbol in SYMBOL_MAP.keys():
        df5  = fetch_ohlcv(symbol, "5m",  period="5d")
        df15 = fetch_ohlcv(symbol, "15m", period="5d")
        df1h = fetch_ohlcv(symbol, "1h",  period="10d")

        if df5 is None or df15 is None:
            print(f"[scanner] {symbol}: data unavailable, skipping")
            continue

        if df1h is None:
            df1h = df15

        for strategy_name, detector in STRATEGY_DETECTORS.items():
            # Check if strategy trades this market
            allowed = STRATEGY_MARKETS.get(strategy_name, list(SYMBOL_MAP.keys()))
            if symbol not in allowed:
                continue

            # ── Self-learning check: skip disabled strategies ──────────────
            if not is_strategy_enabled(strategy_name, symbol):
                print(f"[scanner] {strategy_name}/{symbol}: disabled by self-learning, skipping")
                continue

            # Skip if recently fired (cooldown)
            if _is_duplicate(symbol, strategy_name):
                continue

            try:
                direction, reason = detector(df5, df15, df1h)
            except Exception as e:
                print(f"[scanner] {strategy_name}/{symbol} detector error: {e}")
                continue

            if not direction:
                continue

            signal = {
                "symbol":        symbol,
                "action":        direction,
                "strategy":      strategy_name,
                "timeframe":     "5",
                "price":         round(float(df5["close"].iloc[-1]), 2),
                "source":        "scanner",
                "reason":        reason,
                "ai_confidence": 0.7,
                "regime":        "UNKNOWN",
                "secret":        WEBHOOK_SECRET,
            }

            print(f"[scanner] ✦ {symbol} {direction.upper()} via {strategy_name}: {reason}")

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(WEBHOOK_URL, json=signal)
                    if resp.status_code == 200:
                        _mark_fired(symbol, strategy_name)
                        signals_fired += 1
                    else:
                        print(f"[scanner] webhook returned {resp.status_code}")
            except Exception as e:
                print(f"[scanner] webhook error: {e}")

    print(f"[scanner] ── scan complete. {signals_fired} signal(s) fired ──")


async def start_scanner():
    print(f"[scanner] starting. scanning every {SCAN_INTERVAL//60} minutes.")
    print(f"[scanner] markets: {', '.join(SYMBOL_MAP.keys())}")
    print(f"[scanner] strategies: {len(STRATEGY_DETECTORS)} total")
    while True:
        try:
            await run_single_scan()
        except Exception as e:
            print(f"[scanner] scan loop error: {e}")
        await asyncio.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_single_scan())
