# -*- coding: utf-8 -*-
"""
paper_trade_data.py
"""
from dataclasses import dataclass, field


@dataclass
class candle_snapshot:
    timestamp: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    vwap: float = 0.0

    def to_row(self):
        return [self.timestamp, self.open, self.high, self.low, self.close, self.vwap]


@dataclass
class exit_hit:
    future_price: float = 0.0
    option_price: float = 0.0
    timestamp: str = ""
    is_hit: bool = False

    def to_row(self):
        return [self.future_price, self.option_price, self.timestamp]


@dataclass
class eod_exit:
    future_price: float = 0.0
    option_price: float = 0.0

    def to_row(self):
        return [self.future_price, self.option_price]


@dataclass
class paper_trade_row:
    date: str = ""
    future: str = ""
    option_name: str = ""
    trade_type: str = ""

    piercing_candle: candle_snapshot = field(default_factory=candle_snapshot)
    reclaim_candle: candle_snapshot = field(default_factory=candle_snapshot)
    confirm_candle: candle_snapshot = field(default_factory=candle_snapshot)

    entry_future_price: float = 0.0
    entry_option_price: float = 0.0
    entry_timestamp: str = ""

    # SL (Low of Piercing Candle)
    sl_low: float = 0.0

    # SL as an exit_hit record (recorded when SL is hit)
    sl_hit: exit_hit = field(default_factory=exit_hit)

    # Standard exits
    exit1_hit: exit_hit = field(default_factory=exit_hit)  # Length Of Piercing
    exit2_hit: exit_hit = field(default_factory=exit_hit)  # 2 x Length Of Piercing
    exit3_hit: exit_hit = field(default_factory=exit_hit)  # 8 Points
    # Exit-4 (Bollinger target) -- kept for the engine's hypothesis tracking
    exit4_hit: exit_hit = field(default_factory=exit_hit)

    # Multiple fixed-point exits (user listed several "Exit-4" variants)
    exit4_10_hit: exit_hit = field(default_factory=exit_hit)  # 10 Points
    exit4_15_hit: exit_hit = field(default_factory=exit_hit)  # 15 Points
    exit4_20_hit: exit_hit = field(default_factory=exit_hit)  # 20 Points
    exit4_30_hit: exit_hit = field(default_factory=exit_hit)  # 30 Points
    exit4_40_hit: exit_hit = field(default_factory=exit_hit)  # 40 Points

    # End-of-day exit
    exit5_eod: eod_exit = field(default_factory=eod_exit)

    mae: float = 0.0
    mae_time: str = ""
    mfe: float = 0.0
    mfe_time: str = ""

    # Additional metadata
    interval: str = ""
    best_case_exit: str = ""

    def to_sheet_row(self):
        row = [self.date, self.option_name, self.trade_type]

        # Piercing, Reclaim, Confirm candles
        row += self.piercing_candle.to_row()
        row += self.reclaim_candle.to_row()
        row += self.confirm_candle.to_row()

        # Entry and SL
        row += [self.entry_timestamp]
        row += [self.sl_low]

        # Standard exit hits (each returns [future_price, option_price, timestamp])
        row += self.exit1_hit.to_row()
        row += self.exit2_hit.to_row()
        row += self.exit3_hit.to_row()

        # Bollinger / Exit-4 hypothesis
        row += self.exit4_hit.to_row()

        # Fixed-point exits
        row += self.exit4_10_hit.to_row()
        row += self.exit4_15_hit.to_row()
        row += self.exit4_20_hit.to_row()
        row += self.exit4_30_hit.to_row()
        row += self.exit4_40_hit.to_row()

        # EOD exit
        row += self.exit5_eod.to_row()

        # MAE / MFE
        row += [self.mae, self.mae_time, self.mfe, self.mfe_time]

        # Interval and Best Case Exit
        row += [self.interval, self.best_case_exit]
        return row
