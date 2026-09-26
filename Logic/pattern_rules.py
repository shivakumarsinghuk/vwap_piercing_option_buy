# -*- coding: utf-8 -*-
"""
pattern_rules.py

Pure, stateless VWAP-piercing pattern predicates shared between the live
LogicVwapPiercingOptions state machine and offline testing/dry-run scripts,
so a dry run against historical candles reflects exactly what the live
engine would decide.
"""
from datetime import datetime, timedelta

from DataTypes.defines import *

# New Piercing candidates are only looked for once VWAP has had time to settle (15 min after
# execution start) and stop being looked for at PIERCING_CUTOFF_TIME. A setup already in progress
# (pierced but not yet entered) at that point is abandoned too -- see is_setup_abandon_time_reached
# -- only an already-open trade (IN_TRADE) runs past this point, until FORCE_EXIT_TIME.
PIERCING_START_DELAY_MINUTES = 15
PIERCING_CUTOFF_TIME = "14:00:00"

# Any trade still open (SL not yet hit) at this time is force-closed at the prevailing price,
# regardless of SL/Exit-1..4 state. This is independent of PIERCING_CUTOFF_TIME/setup-abandonment
# above -- it only ever applies to a trade that has actually entered (IN_TRADE).
FORCE_EXIT_TIME = "14:50:00"

# During SEEK_CONFIRM_ENTRY, entry only fires once price has cleared back through VWAP by at
# least this much -- a bare crossing right on the VWAP line is treated as noise, not a real entry.
ENTRY_MIN_VWAP_GAP = 5


def compute_piercing_start_time(execution_start_time):
    """execution_start_time: 'HH:MM:SS'. Returns 'HH:MM:SS', PIERCING_START_DELAY_MINUTES after it."""
    return (datetime.strptime(execution_start_time, "%H:%M:%S")
            + timedelta(minutes=PIERCING_START_DELAY_MINUTES)).strftime("%H:%M:%S")


def is_piercing_window_open(check_time, piercing_start_time):
    """check_time/piercing_start_time as zero-padded 'HH:MM:SS' strings (safe to compare lexically)."""
    return piercing_start_time <= check_time < PIERCING_CUTOFF_TIME


def is_force_exit_time_reached(check_time):
    """check_time as a zero-padded 'HH:MM:SS' string (safe to compare lexically)."""
    return check_time >= FORCE_EXIT_TIME


def is_setup_abandon_time_reached(check_time):
    """
    check_time as a zero-padded 'HH:MM:SS' string. True once PIERCING_CUTOFF_TIME is reached --
    a setup that has pierced but not yet entered (SEEK_RECLAIM / SEEK_CONFIRM_ENTRY) at this point
    is given up on and reset, same cutoff as new-piercing detection. Does NOT apply to an
    already-open trade (IN_TRADE) -- that's FORCE_EXIT_TIME's job, independently.
    """
    return check_time >= PIERCING_CUTOFF_TIME


def time_of_day(date_time_str):
    """Extracts 'HH:MM:SS' from a full 'DD-MM-YYYY HH:MM:SS' (or similar) timestamp string."""
    return date_time_str.split(" ")[-1]


def piercing_direction(row):
    """
    Returns 'BUY', 'SELL', or None for a candle row (needs OPEN/CLOSE/VWAP). A piercing is a
    candle whose open and close are on opposite sides of VWAP.
    """
    body = abs(float(row[CLOSE_PRICE]) - float(row[OPEN_PRICE]))
    top_wick = float(row[HIGH_PRICE]) - max(float(row[OPEN_PRICE]), float(row[CLOSE_PRICE]))
    if body <= top_wick:
        return None
    if row[OPEN_PRICE] < row[VWAP] and row[CLOSE_PRICE] > row[VWAP]:
        return "BUY"
    if row[OPEN_PRICE] > row[VWAP] and row[CLOSE_PRICE] < row[VWAP]:
        return "SELL"
    return None


def is_reclaimed(row, vwap, direction):
    """True if this candle's close lands back on the opposite side of VWAP from the piercing close."""
    return (row[CLOSE_PRICE] < vwap) if direction == "BUY" else (row[CLOSE_PRICE] > vwap)


def is_vwap_reentry_triggered(check_price, vwap, direction):
    """
    The entry trigger: once Reclaim is confirmed, entry fires when price crosses back through
    VWAP in the original piercing direction by at least ENTRY_MIN_VWAP_GAP (BUY: back above VWAP;
    SELL: back below VWAP). check_price is a candle's Close for the backtest/dry-run
    (candle-driven), or the live LTP for the tick-driven live engine -- same predicate either way.
    """
    return (check_price > vwap + ENTRY_MIN_VWAP_GAP) if direction == "BUY" \
        else (check_price < vwap - ENTRY_MIN_VWAP_GAP)

