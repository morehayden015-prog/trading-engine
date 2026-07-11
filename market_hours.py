"""
market_hours.py — Market hours / weekend guard
Futures markets are closed Saturday all day and Sunday before 23:00 UTC.
Used to skip AI calls and scanning on weekends to save API credits.
"""
from datetime import datetime, timezone


def is_market_open() -> bool:
    """
    Returns True if futures markets are open.
    Closed: Saturday 00:00 UTC → Sunday 23:00 UTC
    """
    now     = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Mon, 5=Sat, 6=Sun

    if weekday == 5:                          # All day Saturday
        return False
    if weekday == 6 and now.hour < 23:        # Sunday before 11pm UTC (6pm EST)
        return False
    return True


def market_status() -> dict:
    now     = datetime.now(timezone.utc)
    open_   = is_market_open()
    return {
        "open":    open_,
        "reason":  "Market open" if open_ else "Weekend — markets closed",
        "utc_day": now.strftime("%A"),
        "utc_time": now.strftime("%H:%M UTC"),
    }
