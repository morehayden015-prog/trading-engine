"""
levels.py — single source of truth for symbol price levels.

TP/SL distances and yfinance tickers used to be copy-pasted into
trade_monitor_agent.py, auto_labeler.py and outcome_labeler.py. The copies
drifted: two of the three were missing every forex pair, so a EURUSD trade
looked up the XAUUSD row and inherited a 4.0-price-unit stop that price at
1.16 could never reach. Anything that needs a level imports it from here.

Distances are in price units, not pips or ticks:
  0.0010 on a 4-decimal FX pair = 10 pips
  1.0 on ES/NQ = one index point
"""

TP_DISTANCES = {
    "XAUUSD": {"TP1": 5.0,    "TP2": 10.0,   "TP3": 15.0,   "SL": 4.0},
    "ES":     {"TP1": 5.0,    "TP2": 10.0,   "TP3": 20.0,   "SL": 6.0},
    "NQ":     {"TP1": 15.0,   "TP2": 30.0,   "TP3": 60.0,   "SL": 20.0},
    "CL":     {"TP1": 0.30,   "TP2": 0.60,   "TP3": 1.00,   "SL": 0.25},
    "EURUSD": {"TP1": 0.0015, "TP2": 0.0030, "TP3": 0.0050, "SL": 0.0012},
    "GBPUSD": {"TP1": 0.0020, "TP2": 0.0040, "TP3": 0.0065, "SL": 0.0016},
    "AUDUSD": {"TP1": 0.0012, "TP2": 0.0025, "TP3": 0.0040, "SL": 0.0010},
    "USDJPY": {"TP1": 0.15,   "TP2": 0.30,   "TP3": 0.50,   "SL": 0.12},
}

# yfinance tickers for every symbol the bot trades.
SYMBOL_MAP = {
    "XAUUSD": "GC=F",
    "ES":     "ES=F",
    "NQ":     "NQ=F",
    "CL":     "CL=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
}


def stop_distances(symbol: str):
    """(SL, TP1) for a symbol, or (None, None) if it isn't in the table."""
    levels = TP_DISTANCES.get(symbol)
    if not levels:
        return None, None
    return levels["SL"], levels["TP1"]
