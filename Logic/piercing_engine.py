# -*- coding: utf-8 -*-
"""
piercing_engine.py

Single engine for the VWAP Piercing Options pattern (Piercing -> Reclaim ->
Confirm/Entry -> SL/Exit-1..4/EOD), used identically by both the live
paper-trade logger and the historical backtest replay. A `mode` (see `Mode`)
tells the engine which of the two it's running as -- LIVE polls a broker's
live quotes/candles on a background thread and writes each completed trade
to PaperTradeData as it closes; BACKTEST replays one historical day's
candles in a single pass and returns the list of completed trades for the
caller to write to BackTestData. Everything about the actual trading rules
(piercing/reclaim/entry conditions, SL and Exit-1..4 level formulas, MAE/MFE
tracking, the 14:50 force-exit cutoff, best-case-exit description) lives
here exactly once, so a rule change can't require touching two engines and
drifting between them -- only the handful of genuinely different
touchpoints (candle/tick sourcing, real option quotes vs none, threads vs a
plain loop, log wording/destination) branch on `mode`.

BUY and SELL setups are tracked as two fully independent state machines (see
_DirectionState) so a piercing in one direction is never blocked or lost
while the other direction already has a setup/trade in progress.

SL is the ONLY real exit -- Exit-1..4 (Length-of-Piercing, 0.2%, 0.75%,
Bollinger) are all parallel hypotheses tracked for comparison only; breaching
one is logged but doesn't close the trade. Any trade still open at 14:50 is
force-closed at the prevailing price regardless of SL/Exit-1..4 state.
"""
import threading
import time
from datetime import datetime, date, timedelta
from enum import Enum

import pandas as pd

from BusinessLogic.interfaces.ILogic import *
from BrokerUtility.pal.utility_manager import *
from Utility.quotes_utility import *
from Utility.utility import compute_vwap, compute_bollinger_bands, get_target_price_by_percentage, generate_weekly_expiry_dates, \
    generate_monthly_expiry_dates
from DataTypes.defines import *
from ..DataTypes.paper_trade_data import paper_trade_row, candle_snapshot, exit_hit
from ..UserInterface.adapter.login.login import *
from ..UserInterface.adapter.config.config import *
from ..UserInterface.gsheet.paper_trade.paper_trade import *
from . import pattern_rules
from .option_selection import select_by_premium, select_cheapest_in_band


class Mode(Enum):
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"


STATE_SEEK_PIERCING = "SEEK_PIERCING"
STATE_SEEK_RECLAIM = "SEEK_RECLAIM"
STATE_SEEK_CONFIRM_ENTRY = "SEEK_CONFIRM_ENTRY"
STATE_IN_TRADE = "IN_TRADE"

STATUS_NO_DATA = "no_data"
STATUS_OK = "ok"

DAY_START_TIME = "09:15:00"
BACKTEST_PIERCING_START_TIME = "09:35:00"
BOLLINGER_PERIOD = 20
BOLLINGER_STD_DEV = 2
# LIVE test mode (executor.py --test_mode true --date ...): the simulated clock starts at this
# time on the test date and runs in fast mode: each pass of the execute loop moves it forward by
# TEST_MODE_STEP_SECONDS instead of sleeping, so a whole day replays in seconds. One minute per
# pass matches the 1-min candles prices are taken from, so no candle is skipped.
TEST_MODE_START_TIME = "09:16:15"
TEST_MODE_STEP_SECONDS = 60

# Fyers' real option-chain-query symbol for each index's underlying (confirmed via
# test_fyers_option_chain.py against the live API: "NSE:NIFTY50-INDEX"). getOptionChain()
# prepends the exchange itself, so only the bare symbol goes here.
CHAIN_UNDERLYING_SYMBOL = {"NIFTY": "NIFTY50-INDEX", "BANKNIFTY": "NIFTYBANK-INDEX"}

# BACKTEST-only historical option lookups are per-symbol calls (no batched chain-quote
# equivalent exists for a past date) -- pace them to stay under the broker's rate limit.
HISTORICAL_OPTION_LOOKUP_DELAY_SECONDS = 0.5

# BACKTEST-only memo of the last strike successfully selected per (index_name, option_type),
# kept at module scope (not per engine instance) because BACKTEST builds a fresh engine per
# trading day -- lets __select_historical_option reuse yesterday's strike as today's starting
# point instead of re-running its wide coarse probe every single day.
_LAST_SELECTED_STRIKE = {}


class _DirectionState:
    """All the mutable pattern/trade-tracking state for one direction (BUY or SELL)."""

    def __init__(self, direction):
        self.direction = direction
        self.state = STATE_SEEK_PIERCING
        self.piercing_candle = None
        self.reclaim_candle = None
        # 5-min reclaim candles carry no VWAP of their own -- the main-interval VWAP they were
        # checked against is tracked separately here.
        self.reclaim_vwap = 0.0
        # LIVE only: ticks are polled every few seconds, but the periodic waiting-for-entry/
        # in-trade status line should only print once per candle close, not every poll -- this
        # tracks the candle timestamp that was last logged for that purpose.
        self.last_logged_candle_ts = None
        # BACKTEST/test mode: timestamp of the last 1-min candle already run through the
        # entry/in-trade minute logic, so no minute is applied twice.
        self.last_bar_ts = None

        self.current_trade: paper_trade_row = None
        self.option_symbol = ""
        self.sl_level = 0.0
        self.exit1_level = 0.0
        self.exit2_level = 0.0
        self.exit3_level = 0.0
        self.exit4_10_level = 0.0
        self.exit4_15_level = 0.0
        self.exit4_20_level = 0.0
        self.exit4_30_level = 0.0
        self.exit4_40_level = 0.0
        self.mae = 0.0
        self.mfe = 0.0
        self.mae_time = ""
        self.mfe_time = ""


class VwapPiercingEngine(ILogic):

    STATE_SEEK_PIERCING = STATE_SEEK_PIERCING
    STATE_SEEK_RECLAIM = STATE_SEEK_RECLAIM
    STATE_SEEK_CONFIRM_ENTRY = STATE_SEEK_CONFIRM_ENTRY
    STATE_IN_TRADE = STATE_IN_TRADE

    # Zebu's fetchOHLC/get_quotes/place_order treat market_type="" as "this is an option
    # symbol, parse strike/expiry out of it via __get_option_name" and market_type="EQ" as
    # "append -EQ". Options are trickier: __get_option_name's re-parse assumes a
    # "<STRIKE><CE|PE>" suffix format, but real Zebu option symbols (from get_option_chain) are
    # "<SYMBOL><DDMONYY><C|P><STRIKE>" -- re-parsing an already-correct symbol through that
    # mismatched format would corrupt it. Since our option symbols always come pre-resolved,
    # a non-"" sentinel bypasses that reparse. Fyers has the OPPOSITE quirk (any non-"", non-"FUT"
    # value gets a "-{market_type}" suffix appended, which corrupts an already-complete Fyers
    # option symbol) -- so the correct sentinel is genuinely broker-specific. Each broker utility
    # class exposes its own correct value via `OPTION_MARKET_TYPE`; see __option_market_type().
    OPTION_MARKET_TYPE = "OPT"  # fallback default if a broker doesn't declare its own

    def __init__(self, mode: Mode, option_type="CE", **kwargs):
        self.mode = mode
        self.option_type = option_type
        self.trade_direction = "BUY"
        # LIVE runs one engine per option type (see interfaces.py) -- tag logs with which one
        self.logic_name = f"LogicVwapPiercingOptionsBuy[{option_type}]"

        # strategy constants
        self.index_name = "NIFTY"
        self.strike_step = 50
        self.target_premium_low = 80.0
        self.target_premium_high = 130.0
        self.bollinger_period = BOLLINGER_PERIOD
        self.bollinger_std_dev = BOLLINGER_STD_DEV

        # Each option type runs the BUY pattern independently; CE and PE are separate
        # instruments, but neither is treated as a SELL strategy.
        self.signal_option_symbol = ""
        # Preselected cheapest options for runtime (CE and PE)
        self.preselected_options = {"CE": (None, 0.0), "PE": (None, 0.0)}
        self.directions = {self.trade_direction: _DirectionState(self.trade_direction)}
        self.last_candle_data = None  # main-interval candle series (with VWAP column)
        self.test_mode = False  # LIVE may switch this on from args (see __init_live)

        if mode == Mode.LIVE:
            self.__init_live(**kwargs)
        else:
            self.__init_backtest(**kwargs)

    # ------------------------------------------------------------------
    # mode-specific setup
    # ------------------------------------------------------------------
    def __init_live(self, args, broker_utility_manager: utility_manager, quotes_utility: QuoteUtility):

        print("Starting Live Mode of Logic: ", self.logic_name)
        self.obj_utility_manager = broker_utility_manager
        self.obj_ui_adapter_login: UserInterfaceAdapterLogin = UserInterfaceAdapterLogin(args)
        self.obj_ui_adapter_config: UserInterfaceAdapterConfig = UserInterfaceAdapterConfig(args)
        self.trade_utility = self.obj_utility_manager.get_utility_object(self.obj_ui_adapter_login.get_data())
        self.quotes_utility: QuoteUtility = quotes_utility
        # wire this up now, before any threads start -- pre_requisite_thread calls
        # quotes_utility.add_stocks() almost immediately, which needs trade_utility to already be
        # set. executor.py also calls set_trade_utility() after create() returns, but that's too
        # late to win the race against pre_requisite_thread; this call makes that one redundant
        # but harmless (idempotent), and fixes the actual race here at the source.
        self.quotes_utility.set_trade_utility(self.trade_utility)
        self.config_data = self.obj_ui_adapter_config.get_data()
        self.obj_paper_trade_writer = UserInterfacePaperTrade(args.key)

        # executor.py --test_mode true --date YYYY-MM-DD: replay that trading date instead of
        # today -- the clock starts at TEST_MODE_START_TIME on that date (see __now), and every
        # candle and price comes from that date's history rather than the live market.
        self.test_mode = bool(getattr(args, "test_mode", False))
        self.session_date_str = args.date if self.test_mode else date.today().strftime("%Y-%m-%d")
        # simulated test-mode clock (see __now / __advance_or_sleep)
        self.test_clock = datetime.strptime(f"{self.session_date_str} {TEST_MODE_START_TIME}",
                                            "%Y-%m-%d %H:%M:%S")
        if self.test_mode:
            print(self.logic_name, ": TEST MODE -- using", self.session_date_str,
                  "with the clock starting at", TEST_MODE_START_TIME)
            # the BACKTEST helpers (__select_historical_option etc.) read these attributes
            self.broker = self.trade_utility.get_broker_utility()
            self.trade_date_str = self.session_date_str
            self.log_fn = lambda msg: print(self.logic_name, ":", msg)
            # (symbol, interval_minutes) -> that symbol's whole test-day candle series
            self.test_symbol_candles = {}

        self.pre_requisite_complete_event = threading.Event()
        self.day_preset = 0

        self.pre_requisite_start_time = (self.__now() + timedelta(seconds=10)).strftime('%H:%M:%S')
        self.execution_start_time = self.config_data.start_time
        self.execution_stop_time = self.config_data.end_time
        self.candle_interval_minutes = int(self.config_data.candle_interval)
        self.piercing_start_time = pattern_rules.compute_piercing_start_time(self.execution_start_time)

        self.processed_candle_count = 0
        self.processed_candle_count_5min = 0

        self.pre_requisite_thread = threading.Thread(target=self.pre_requisite_thread_handler)
        self.execute_thread = threading.Thread(target=self.execute)
        self.exit_thread = threading.Thread(target=self.exit_execution_thread)

        self.pre_requisite_thread.start()
        self.execute_thread.start()
        self.exit_thread.start()

    def __init_backtest(self, broker, index_name, trade_date_str, candle_interval_minutes, log_fn=None):
        self.broker = broker
        self.index_name = index_name
        self.trade_date_str = trade_date_str
        self.candle_interval_minutes = candle_interval_minutes
        self.log_fn = log_fn or (lambda msg: None)
        self.piercing_start_time = BACKTEST_PIERCING_START_TIME
        self.results = []

    def get_broker_utility(self):
        return self.trade_utility

    def get_thread_info(self):
        return self.exit_thread

    # ------------------------------------------------------------------
    # LIVE thread handlers
    # ------------------------------------------------------------------
    def pre_requisite_thread_handler(self):
        print(self.logic_name, ": Inside pre-requisite thread")
        broker = self.trade_utility.get_broker_utility()

        if self.test_mode:
            # today's live chain says nothing about the test date -- pick the cheapest in-band
            # option from that date's own premiums at the simulated start time instead, and
            # load its candles once up front.
            start_ts = f"{self.session_date_str} {TEST_MODE_START_TIME}"
            symbol, price = self.__select_historical_option(self.option_type, start_ts)
            if symbol:
                self.preselected_options[self.option_type] = (symbol, price)
                self.signal_option_symbol = symbol
                for interval_minutes in sorted({self.candle_interval_minutes, 5, 1}):
                    self.__test_day_series(broker, symbol, interval_minutes)
            print(self.logic_name, ": TEST MODE -- selected option:", self.preselected_options[self.option_type])
            self.pre_requisite_complete_event.set()
            print(self.logic_name, ": Exiting pre-requisite thread")
            return

        # Pre-select cheapest CE and PE in the target premium band using option chain
        try:
            underlying = CHAIN_UNDERLYING_SYMBOL.get(self.index_name, self.index_name)
            chain_df, _, _ = broker.getOptionChain(underlying)
            ce_sym, ce_price = select_cheapest_in_band(chain_df, "CE", self.target_premium_low, self.target_premium_high)
            pe_sym, pe_price = select_cheapest_in_band(chain_df, "PE", self.target_premium_low, self.target_premium_high)
            # fetchOHLC/get_quotes add the exchange themselves -- keep symbols bare, or Fyers is
            # asked for "NSE:NSE:..." and rejects it.
            if ce_sym:
                self.preselected_options["CE"] = (ce_sym.split(":")[-1], ce_price)
            if pe_sym:
                self.preselected_options["PE"] = (pe_sym.split(":")[-1], pe_price)
            self.signal_option_symbol = self.preselected_options.get(self.option_type, (None, 0.0))[0]
            # ensure quotes utility is subscribed to option quotes for live monitoring
            option_symbols = [s for s, p in self.preselected_options.values() if s]
            if option_symbols:
                self.quotes_utility.add_stocks(option_symbols, [self.__option_market_type()] * len(option_symbols))
            print(self.logic_name, ": Preselected options CE/PE:", self.preselected_options)
        except Exception:
            # non-fatal: fall back to selecting at entry time as before
            print(self.logic_name, ": Option chain lookup failed; will select at entry time")
        self.pre_requisite_complete_event.set()
        print(self.logic_name, ": Exiting pre-requisite thread")

    def execute(self):
        self.pre_requisite_complete_event.wait()
        print(self.logic_name, ": Execution Started", self.execution_start_time)
        has_started = False
        piercing_window_announced = False

        while not self.__is_time_reached(self.execution_stop_time):
            if not self.__is_time_reached(self.execution_start_time):
                self.__advance_or_sleep(2)
                continue

            if not has_started:
                print(self.logic_name, f": [{self.__now().strftime('%H:%M:%S')}] execution start time reached, beginning candle/tick processing")
                has_started = True
            if not piercing_window_announced and self.__is_piercing_window_open():
                print(self.logic_name, f": [{self.__now().strftime('%H:%M:%S')}] piercing window open (>= {self.piercing_start_time})")
                piercing_window_announced = True

            now_str = self.__now().strftime("%Y-%m-%d %H:%M:%S")
            self.__process_new_candles_live(now_str)

            for ds in self.directions.values():
                if ds.state in (STATE_SEEK_RECLAIM, STATE_SEEK_CONFIRM_ENTRY) \
                        and self.__check_abandon_incomplete_setup(ds, now_str):
                    continue
                if ds.state == STATE_SEEK_CONFIRM_ENTRY:
                    self.__check_entry_trigger_live(ds)
                elif ds.state == STATE_IN_TRADE:
                    self.__check_exit_hits_live(ds)

            self.__advance_or_sleep(3)

        for ds in self.directions.values():
            if ds.state == STATE_IN_TRADE:
                self.__finalize_trade_at_eod_live(ds)

        print(self.logic_name, ": Execution loop ended")

    def exit_execution_thread(self):
        print(self.logic_name, ": Start of Exiting Thread")
        self.execute_thread.join()
        self.quotes_utility.stop()
        self.quotes_utility.get_thread_info().join()
        print(self.logic_name, ": End of Exiting Thread")

    # ------------------------------------------------------------------
    # LIVE candle/tick sourcing
    # ------------------------------------------------------------------
    def __process_new_candles_live(self, now_str):
        # now_str ("YYYY-MM-DD HH:MM:SS"): this pass's time on the session date (the --date in
        # test mode, else today).
        broker = self.trade_utility.get_broker_utility()
        # zebumynt_utitlity.getTimeFrame() returns epoch-second strings (via strftime('%s'), which
        # isn't even portable on Windows) -- but fetchOHLC expects "YYYY-MM-DD HH:MM:SS" and does
        # its own epoch conversion internally, so build the strings directly instead, matching the
        # backtest/dry-run scripts' already-proven-working pattern.
        str_from_date = f"{self.session_date_str} 09:15:00"
        str_to_date = now_str
        signal_symbol = self.signal_option_symbol or self.preselected_options.get(self.option_type, (None, 0.0))[0]
        if not signal_symbol:
            return
        candle_data = self.__fetch_candles_live(broker, signal_symbol, self.candle_interval_minutes,
                                                str_from_date, str_to_date)
        if candle_data is not None and len(candle_data) > 0:
            # Zebu's "intvwap" field is a per-candle (interval) VWAP, not a cumulative session VWAP
            # from day open -- confirmed by its volatility mirroring price itself rather than
            # smoothing out as the session progresses. The Piercing/Reclaim pattern needs the real
            # cumulative session VWAP, so always compute it ourselves rather than trusting intvwap.
            candle_data[VWAP] = [compute_vwap(candle_data, last_loc=i + 1) for i in range(len(candle_data))]

            self.last_candle_data = candle_data

            new_rows = candle_data.iloc[self.processed_candle_count:]
            for _, row in new_rows.iterrows():
                for ds in self.directions.values():
                    if ds.state == STATE_SEEK_PIERCING:
                        ts = str(row[DATE_TIME])
                        self.__check_seek_piercing(ds, row, ts)

            self.processed_candle_count = len(candle_data)

        # Reclaim is checked against 5-min candles while still measured against the main
        # interval's own VWAP (self.last_candle_data), not a separate 5-min VWAP.
        candle_data_5min = self.__fetch_candles_live(broker, signal_symbol, 5, str_from_date, str_to_date)
        if candle_data_5min is not None and len(candle_data_5min) > 0:
            new_rows_5min = candle_data_5min.iloc[self.processed_candle_count_5min:]
            for _, row in new_rows_5min.iterrows():
                for ds in self.directions.values():
                    if ds.state == STATE_SEEK_RECLAIM:
                        self.__on_reclaim_5min_close_live(ds, row)

            self.processed_candle_count_5min = len(candle_data_5min)

    def __on_reclaim_5min_close_live(self, ds: _DirectionState, row):
        # row is a 5-min candle; vwap is looked up from the main-interval series so reclaim is
        # still measured against the same VWAP the rest of the pattern uses.
        current_vwap = self.__get_latest_vwap()
        if current_vwap is None:
            return
        ts = str(row[DATE_TIME])
        if self.__check_reclaim(ds, row, current_vwap, ts):
            return
        # Not reclaimed yet -- keep waiting on subsequent 5-min candles rather than abandoning
        # after just one miss. The piercing candle stays the reference point.
        print(self.logic_name, f": [{ts}] ({ds.direction}) no reclaim yet, still waiting {self.__fmt_candle(row, current_vwap)}")

    def __check_entry_trigger_live(self, ds: _DirectionState):
        if self.test_mode:
            self.__check_entry_trigger_test(ds)
            return
        signal_symbol = self.signal_option_symbol or self.preselected_options.get(self.option_type, (None, 0.0))[0]
        ltp = self.__ltp(signal_symbol)
        if ltp is None:
            return
        current_vwap = self.__get_latest_vwap()
        if current_vwap is None:
            return
        now_str = self.__now().strftime("%H:%M:%S")
        # Entry triggers when option LTP reaches the option piercing candle's extreme.
        if ds.piercing_candle is None:
            return
        piercing_high = float(ds.piercing_candle[HIGH_PRICE])
        piercing_low = float(ds.piercing_candle[LOW_PRICE])
        if not self.__has_acceptable_top_wick(self.last_candle_data.iloc[-1]):
            self.__log_once_per_candle(ds, f": [{now_str}] ({ds.direction}) waiting for entry: confirmation candle top wick exceeds body")
            return
        if ds.direction == "BUY":
            triggered = ltp >= piercing_high
        else:
            triggered = ltp <= piercing_low
        if triggered:
            # a real event -- always print, not subject to the once-per-candle throttle below.
            print(self.logic_name, f": [{now_str}] ({ds.direction}) waiting for entry: LTP={ltp} vs VWAP={current_vwap:.2f} -> TRIGGERED")
        else:
            self.__log_once_per_candle(ds, f": [{now_str}] ({ds.direction}) waiting for entry: LTP={ltp} vs VWAP={current_vwap:.2f}")
        if not triggered:
            return
        self.__enter_trade(ds, ltp, now_str)

    def __get_latest_vwap(self):
        if self.last_candle_data is None or len(self.last_candle_data) == 0:
            return None
        return float(self.last_candle_data[VWAP].iloc[-1])

    def __latest_candle_ts(self):
        if self.last_candle_data is None or len(self.last_candle_data) == 0:
            return None
        return str(self.last_candle_data[DATE_TIME].iloc[-1])

    def __log_once_per_candle(self, ds: _DirectionState, message):
        # ticks come in every few seconds, but this status line should only print once per candle
        # close -- suppress repeats until the underlying candle data actually advances.
        candle_ts = self.__latest_candle_ts()
        if candle_ts is not None and candle_ts == ds.last_logged_candle_ts:
            return
        if candle_ts is not None:
            ds.last_logged_candle_ts = candle_ts
        print(self.logic_name, message)

    def __check_exit_hits_live(self, ds: _DirectionState):
        if self.test_mode:
            self.__check_exit_hits_test(ds)
            return
        option_ltp = self.__ltp(ds.option_symbol)
        if option_ltp is None:
            return
        now_str = self.__now().strftime("%H:%M:%S")

        self.__update_mae_mfe_point(ds, option_ltp, now_str)

        # any trade still open at 14:50 is force-closed at the prevailing price, regardless of
        # SL/Exit-1..4 state.
        if pattern_rules.is_force_exit_time_reached(now_str):
            self.__finalize_trade_at_eod_live(ds, "Force Exit 14:50")
            return

        was_hit = self.__snapshot_exit_hits(ds.current_trade)

        self.__mark_exit_if_hit_point(ds.current_trade.sl_hit, ds.sl_level, option_ltp, now_str, is_stop=True)
        self.__mark_exit_if_hit_point(ds.current_trade.exit1_hit, ds.exit1_level, option_ltp, now_str)
        self.__mark_exit_if_hit_point(ds.current_trade.exit2_hit, ds.exit2_level, option_ltp, now_str)
        self.__mark_exit3_if_hit_point(ds, option_ltp, now_str)
        self.__mark_exit_if_hit_point(ds.current_trade.exit4_10_hit, ds.exit4_10_level, option_ltp, now_str,
                          record_target_price=True)
        self.__mark_exit_if_hit_point(ds.current_trade.exit4_15_hit, ds.exit4_15_level, option_ltp, now_str,
                          record_target_price=True)
        self.__mark_exit_if_hit_point(ds.current_trade.exit4_20_hit, ds.exit4_20_level, option_ltp, now_str,
                          record_target_price=True)
        self.__mark_exit_if_hit_point(ds.current_trade.exit4_30_hit, ds.exit4_30_level, option_ltp, now_str,
                          record_target_price=True)
        self.__mark_exit_if_hit_point(ds.current_trade.exit4_40_hit, ds.exit4_40_level, option_ltp, now_str,
                          record_target_price=True)

        self.__log_exit_breaches(ds, was_hit)

        self.__log_once_per_candle(ds, f": [{now_str}] ({ds.direction}) in-trade LTP={option_ltp} SL={ds.sl_level} "
             f"Exit1={ds.exit1_level:.2f} Exit2={ds.exit2_level:.2f} Exit3={ds.exit3_level:.2f} "
             f"(option-price based)")

        # SL is the one real exit here. Once it fires, the position is closed for real, so log
        # the trade now and go back to scanning for the next Piercing setup -- not capped at
        # one trade per day.
        if ds.current_trade.sl_hit.is_hit:
            self.__stamp_mae_mfe(ds)
            self.__finalize_and_reset_live(ds, "SL")

    def __mark_exit_if_hit_point(self, exit_hit_obj: exit_hit, level, option_ltp, now_str,
                                 is_stop=False, record_target_price=False):
        # LIVE checks a single LTP point against the level (tick-driven; no High/Low range).
        if exit_hit_obj.is_hit or level == 0.0:
            return
        if is_stop:
            hit = option_ltp <= level
        else:
            hit = option_ltp >= level
        if hit:
            exit_hit_obj.option_price = level if record_target_price else option_ltp
            exit_hit_obj.timestamp = now_str
            exit_hit_obj.is_hit = True

    def __mark_exit3_if_hit_point(self, ds: _DirectionState, option_ltp, now_str):
        exit_hit_obj = ds.current_trade.exit3_hit
        if exit_hit_obj.is_hit or ds.exit3_level == 0.0:
            return
        if option_ltp >= ds.exit3_level:
            exit_hit_obj.option_price = ds.current_trade.entry_option_price + 7.0
            exit_hit_obj.timestamp = now_str
            exit_hit_obj.is_hit = True

    def __get_bollinger_exit_level_live(self, ds: _DirectionState):
        # Exit-4 target: Bollinger upper band for BUY, lower band for SELL -- price reaching the
        # band in the trade's favor, same target-style semantics as Exit-1..3 (not a stop).
        if self.last_candle_data is None or len(self.last_candle_data) < self.bollinger_period:
            return None
        upper_band, middle_band, lower_band = compute_bollinger_bands(self.last_candle_data,
                                                                       period=self.bollinger_period,
                                                                       std_dev=self.bollinger_std_dev)
        band_value = upper_band.iloc[-1] if ds.direction == "BUY" else lower_band.iloc[-1]
        if band_value != band_value:  # NaN check without importing pandas/numpy here
            return None
        return float(band_value)

    def __finalize_trade_at_eod_live(self, ds: _DirectionState, reason="EOD"):
        option_ltp = self.__ltp(ds.option_symbol)
        if option_ltp is None:
            option_ltp = ds.current_trade.entry_option_price

        ds.current_trade.exit5_eod.option_price = option_ltp
        ds.current_trade.exit5_eod.timestamp = self.__now().strftime("%H:%M:%S")
        self.__stamp_mae_mfe(ds)

        self.__finalize_and_reset_live(ds, reason)

    def __finalize_and_reset_live(self, ds: _DirectionState, reason="EOD"):
        print(self.logic_name, f": FINALIZING LIVE TRADE reason={reason} direction={ds.direction} option={ds.option_symbol} entry={ds.current_trade.entry_trigger_price} ts={ds.current_trade.entry_timestamp}")
        self.obj_paper_trade_writer.write_trade(ds.current_trade, self.candle_interval_minutes,
                                                describe_exit_outcomes(ds.current_trade))
        close_option_price = ds.current_trade.sl_hit.option_price if reason == "SL" \
            else ds.current_trade.exit5_eod.option_price
        print(self.logic_name, f": ({ds.direction}) Trade closed ({reason}) @ {self.__fmt_option_price(close_option_price)}, logged to PaperTradeData:",
             ds.current_trade.trade_type, ds.current_trade.option_name)
        ds.current_trade = None
        ds.option_symbol = ""
        ds.piercing_candle = None
        ds.reclaim_candle = None
        ds.last_logged_candle_ts = None
        # if the piercing window is already closed (past 14:30 or before start+15min at EOD),
        # this just idles in SEEK_PIERCING harmlessly -- __check_seek_piercing gates on the window.
        ds.state = STATE_SEEK_PIERCING

    def __option_market_type(self):
        broker = self.trade_utility.get_broker_utility() if self.mode == Mode.LIVE else self.broker
        return getattr(broker, "OPTION_MARKET_TYPE", self.OPTION_MARKET_TYPE)

    def __select_option_by_premium(self, broker, option_type):
        # Fyers' real getOptionChain() returns strike/type/live-premium for every contract around
        # the current ATM in one call (no separate get_quotes step needed, unlike the old
        # per-strike-symbol + batch-quote approach).
        underlying = CHAIN_UNDERLYING_SYMBOL.get(self.index_name, self.index_name)
        chain_df, _, _ = broker.getOptionChain(underlying)
        symbol, price = select_cheapest_in_band(chain_df, option_type, self.target_premium_low, self.target_premium_high)
        return (symbol.split(":")[-1] if symbol else symbol), price

    def __select_historical_option(self, option_type, ts):
        """
        BACKTEST only, option-price only: no underlying price is used anywhere in this
        search -- only the option contracts' own historical closes decide which one gets
        selected. Finds the cheapest weekly contract of option_type whose historical close near
        ts falls inside [target_premium_low, target_premium_high].

        There's no historical equivalent of getOptionChain (it only ever answers "what's the
        premium right now"), and probing every possible strike every call is far too many
        rate-limited historical fetches (see HISTORICAL_OPTION_LOOKUP_DELAY_SECONDS). Instead:
        a "center" strike is memoized per (index, option_type) across calls in this process
        (_LAST_SELECTED_STRIKE) -- the underlying rarely moves far day-to-day, so most calls
        only need the narrow ~40-candidate scan around wherever the previous call last found the
        option. The first-ever call for a given index/option_type seeds that center from the
        live option chain instead (the strike of today's cheapest in-band option -- option data
        only). If the fine scan finds nothing in-band around the center, __locate walks from it
        in the direction the option's own premium points (only ever near-the-money strikes, so
        it never requests unlisted far-away ones) until it's close to the band.
        """
        trade_date = datetime.strptime(self.trade_date_str, "%Y-%m-%d")
        weekly_expiry = generate_weekly_expiry_dates(trade_date, 1)[0]
        # the last weekly expiry of a month IS the monthly expiry, and brokers name it with the
        # monthly format (Fyers: NIFTY26SEP23200PE, not NIFTY2692923200PE -- the latter is
        # rejected as "Invalid symbol provided").
        is_month_expiry = weekly_expiry in generate_monthly_expiry_dates(trade_date, 1)

        def probe(strike):
            symbol = self.broker.get_option_name(self.index_name, weekly_expiry, is_month_expiry, str(strike), option_type)
            return symbol, self.__historical_option_close_near(symbol, ts)

        def fine_scan(center_strike):
            best_symbol, best_price, best_strike = None, 0.0, None
            for offset in range(-20, 21):
                strike = center_strike + offset * self.strike_step
                symbol, price = probe(strike)
                if price is None or price <= 0:
                    continue
                if self.target_premium_low <= price <= self.target_premium_high:
                    if best_symbol is None or price < best_price:
                        best_symbol, best_price, best_strike = symbol, price, strike
            return best_symbol, best_price, best_strike

        def seed_from_chain():
            # Today's option chain (option prices only): the strike of the cheapest option of
            # this type currently inside the premium band.
            try:
                underlying = CHAIN_UNDERLYING_SYMBOL.get(self.index_name, self.index_name)
                chain_df, _, _ = self.broker.getOptionChain(underlying)
                symbol, _ = select_cheapest_in_band(chain_df, option_type,
                                                    self.target_premium_low, self.target_premium_high)
                if not symbol or "strike_price" not in chain_df.columns:
                    return None
                # compare without any exchange prefix ("NSE:...") on either side
                bare = str(symbol).split(":")[-1]
                row = chain_df[chain_df["symbol"].astype(str).str.split(":").str[-1] == bare]
                return int(float(row.iloc[0]["strike_price"])) if len(row) else None
            except Exception:
                return None

        def locate(start_strike):
            # Walk from start_strike toward the premium band using only the option's own
            # premium: a CE's premium falls as the strike rises, a PE's rises with it. Stops once
            # the premium is near the band -- the fine scan (+/-20 strikes) then picks the exact
            # cheapest in-band contract. Step halves whenever the walk overshoots and turns back.
            strike, step, direction = start_strike, 5 * self.strike_step, 0
            for _ in range(40):
                _, price = probe(strike)
                if price is not None and price > 0:
                    if self.target_premium_low / 2 <= price <= self.target_premium_high * 2:
                        return strike
                    too_rich = price > self.target_premium_high
                    new_direction = (1 if too_rich else -1) * (1 if option_type == "CE" else -1)
                    if direction and new_direction != direction:
                        step = max(self.strike_step, step // 2)
                    direction = new_direction
                elif not direction:
                    # no data at the very start (e.g. a thinly traded strike) -- nudge toward
                    # cheaper/OTM strikes until something prices
                    direction = 1 if option_type == "CE" else -1
                strike += direction * step
            return None

        key = (self.index_name, option_type)
        center = _LAST_SELECTED_STRIKE.get(key) or seed_from_chain()
        if center is None:
            self.__log(f"[{ts}] Could not seed an option strike (no previous selection and no "
                      f"in-band option in the current option chain)")
            return None, 0.0

        # locate first (one probe if the center is already near the band) so a stale center
        # doesn't cost a full 41-strike fine scan before the miss is noticed
        located = locate(center)
        if located is None:
            self.__log(f"[{ts}] No {option_type} option near the "
                      f"{self.target_premium_low:.0f}-{self.target_premium_high:.0f} band for "
                      f"expiry {weekly_expiry} (likely delisted/no trading this day)")
            return None, 0.0
        best_symbol, best_price, best_strike = fine_scan(located)

        if best_symbol is not None:
            _LAST_SELECTED_STRIKE[key] = best_strike
        return best_symbol, best_price

    def __historical_option_close_near(self, option_symbol, ts):
        # BACKTEST only: the Close of the option's own 1-min candle nearest (at or before) ts --
        # "nearest" here, not an exact real-time LTP, since no intrabar ticks exist historically.
        if not option_symbol:
            return None
        str_from = f"{self.trade_date_str} {DAY_START_TIME}"
        # entry selection alone fires ~40 of these back-to-back (one per candidate strike) --
        # paced to avoid tripping the broker's per-second rate limit (seen in practice: Fyers
        # returning HTTP 429 "request limit reached" without this).
        time.sleep(HISTORICAL_OPTION_LOOKUP_DELAY_SECONDS)
        data = self.broker.fetchOHLC(option_symbol, str_from, ts, interval="1minute",
                                     all_data=True, market_type=self.__option_market_type())
        if data is None or len(data) == 0:
            return None
        # Some brokers (Fyers confirmed) only support DATE-granularity historical range filters
        # and silently ignore the time-of-day portion of str_to_date -- returning the WHOLE day's
        # candles regardless of ts. Trusting the broker to have already cut it off at ts would
        # (and did) return the same end-of-day candle for every lookup on a given day, no matter
        # when ts actually was. Filter down to the candle nearest (at or before) ts ourselves.
        # Compared on time-of-day only (all rows are this one trade date) so it holds regardless
        # of which date format ts or the broker's DATE_TIME column happens to use.
        cutoff = pattern_rules.time_of_day(ts)
        filtered = data[data[DATE_TIME].astype(str).map(pattern_rules.time_of_day) <= cutoff]
        if len(filtered) == 0:
            return None
        return float(filtered.iloc[-1][CLOSE_PRICE])

    def __is_piercing_window_open(self):
        return pattern_rules.is_piercing_window_open(self.__now().strftime("%H:%M:%S"),
                                                      self.piercing_start_time)

    def __is_time_reached(self, str_time):
        target_time = datetime.strptime(str_time, "%H:%M:%S").time()
        return self.__now().time() >= target_time

    def __now(self):
        # LIVE clock: real time, or in test mode the simulated clock, which only moves when the
        # execute loop advances it (fast mode).
        return self.test_clock if self.test_mode else datetime.now()

    def __advance_or_sleep(self, seconds):
        # end of an execute-loop pass: test mode (fast mode) jumps the simulated clock ahead
        # instead of waiting; normal LIVE sleeps.
        if self.test_mode:
            self.test_clock += timedelta(seconds=TEST_MODE_STEP_SECONDS)
        else:
            time.sleep(seconds)

    def __test_day_series(self, broker, symbol, interval_minutes):
        # test mode: symbol's whole test-day candle series at this interval, fetched once
        key = (symbol, interval_minutes)
        if key not in self.test_symbol_candles:
            data = broker.fetchOHLC(symbol, f"{self.session_date_str} 09:15:00",
                                    f"{self.session_date_str} 15:30:00",
                                    interval=f"{interval_minutes}minute", all_data=True,
                                    market_type=self.__option_market_type())
            self.test_symbol_candles[key] = data
            print(self.logic_name, f": TEST MODE -- loaded {0 if data is None else len(data)} "
                                   f"{interval_minutes}-min candles of {symbol}")
        return self.test_symbol_candles[key]

    def __closed_by(self, day, interval_minutes, now_str):
        # the rows of a test-day candle series that have fully closed by now_str (a candle
        # starting at T closes at T + interval), so the replay never sees later prices
        if day is None or len(day) == 0:
            return day
        starts = pd.to_datetime(self.session_date_str + " "
                                + day[DATE_TIME].astype(str).map(pattern_rules.time_of_day))
        closes = starts + pd.Timedelta(minutes=interval_minutes)
        now = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
        return day[(closes <= now).values].reset_index(drop=True).copy()

    def __fetch_candles_live(self, broker, symbol, interval_minutes, str_from_date, now_str):
        # Normal LIVE: ask the broker for today's candles up to now. Test mode: no broker call --
        # the test day's candles (fetched once) that have closed by the simulated time.
        if not self.test_mode:
            return broker.fetchOHLC(symbol, str_from_date, now_str,
                                    interval=f"{interval_minutes}minute", all_data=True,
                                    market_type=self.__option_market_type())
        return self.__closed_by(self.__test_day_series(broker, symbol, interval_minutes),
                                interval_minutes, now_str)

    def __last_closed_bar(self, symbol):
        # test mode: the symbol's most recent 1-min candle closed by the simulated clock, or None
        rows = self.__fetch_candles_live(self.trade_utility.get_broker_utility(), symbol, 1, None,
                                         self.__now().strftime("%Y-%m-%d %H:%M:%S"))
        if rows is None or len(rows) == 0:
            return None
        return rows.iloc[-1]

    def __ltp(self, symbol):
        # Price of symbol right now, or None if unavailable. Normal LIVE: the live quote. Test
        # mode: close of its last 1-min candle closed by the simulated clock -- never the live
        # market.
        if not symbol:
            return None
        if not self.test_mode:
            quote = self.quotes_utility.get_quote_data().get(symbol)
            return quote.ltp if quote is not None else None
        bar = self.__last_closed_bar(symbol)
        return None if bar is None else float(bar[CLOSE_PRICE])

    # ------------------------------------------------------------------
    # BACKTEST driver
    # ------------------------------------------------------------------
    def run_backtest_day(self):
        """
        Returns (list_of_paper_trade_row, option_symbol, status) for one historical trading day.
        Mirrors the live engine's state machine exactly (same shared handler methods below) but
        replays a single pre-fetched day of candles in one pass instead of polling threads.
        """
        broker = self.broker

        str_from_date = f"{self.trade_date_str} {DAY_START_TIME}"
        str_to_date = f"{self.trade_date_str} 15:30:00"

        # Select the BUY option from the options' own opening premiums, then run the pattern
        # entirely on that option's candles.
        signal_ts = str_from_date
        signal_option_symbol, _ = self.__select_historical_option(self.option_type, signal_ts)
        if not signal_option_symbol:
            return [], "", STATUS_NO_DATA
        self.signal_option_symbol = signal_option_symbol
        option_candle_data = broker.fetchOHLC(
            signal_option_symbol, str_from_date, str_to_date,
            interval=f"{self.candle_interval_minutes}minute", all_data=True,
            market_type=self.__option_market_type())
        if option_candle_data is None or len(option_candle_data) == 0:
            return [], signal_option_symbol, STATUS_NO_DATA
        candle_data = option_candle_data

        # Zebu's "intvwap" field is a per-candle (interval) VWAP, not a cumulative session VWAP
        # from day open -- always compute the real cumulative VWAP ourselves instead of trusting it.
        candle_data[VWAP] = [compute_vwap(candle_data, last_loc=i + 1) for i in range(len(candle_data))]
        self.last_candle_data = candle_data

        # Reclaim is checked against 5-min candles while still measured against the main
        # interval's own VWAP, not a separate 5-min VWAP. Falls back to the main-interval
        # series if 5-min data isn't available.
        candle_data_5min = broker.fetchOHLC(signal_option_symbol, str_from_date, str_to_date,
                            interval="5minute", all_data=True,
                            market_type=self.__option_market_type())
        if candle_data_5min is None or len(candle_data_5min) == 0:
            candle_data_5min = candle_data

        # Entry and everything after it run minute by minute on the option's 1-min candles, the
        # same way LIVE test mode does (see __stop_entry_fill / __in_trade_minute).
        self.minute_data = broker.fetchOHLC(signal_option_symbol, str_from_date, str_to_date,
                                            interval="1minute", all_data=True,
                                            market_type=self.__option_market_type())
        if self.minute_data is None or len(self.minute_data) == 0:
            self.__log(f"No 1-min candles for {signal_option_symbol} -- entries can't be evaluated this day")
            self.minute_data, self.minute_starts = None, []
        else:
            self.minute_starts = [self.__candle_start(self.minute_data.iloc[k]) for k in range(len(self.minute_data))]

        # Bollinger bands are rolling (causal, only look back), so precomputing over the whole day
        # upfront and indexing by row is equivalent to recomputing fresh at each candle -- no
        # lookahead bias.
        self.upper_band, self.middle_band, self.lower_band = compute_bollinger_bands(
            candle_data, period=self.bollinger_period, std_dev=self.bollinger_std_dev)

        five_min_idx = 0

        for i in range(len(candle_data)):
            row = candle_data.iloc[i]
            ts = str(row[DATE_TIME])
            self._backtest_row_index = i

            # 5-min candles closing within this main-interval candle's window, consumed in order
            # for the reclaim and confirmation checks.
            sub_rows = []
            while five_min_idx < len(candle_data_5min) and str(candle_data_5min.iloc[five_min_idx][DATE_TIME]) <= ts:
                sub_rows.append(candle_data_5min.iloc[five_min_idx])
                five_min_idx += 1

            for direction, ds in self.directions.items():
                if ds.state in (STATE_SEEK_RECLAIM, STATE_SEEK_CONFIRM_ENTRY) \
                        and self.__check_abandon_incomplete_setup(ds, ts):
                    continue

                if ds.state == STATE_SEEK_PIERCING:
                    self.__check_seek_piercing(ds, row, ts)

                elif ds.state == STATE_SEEK_RECLAIM:
                    self.__check_reclaim_backtest(ds, row, sub_rows, ts)

                elif ds.state == STATE_SEEK_CONFIRM_ENTRY:
                    self.__check_confirm_entry_backtest(ds, row, sub_rows, ts)

                elif ds.state == STATE_IN_TRADE:
                    self.__check_in_trade_backtest(ds, row, ts)

        # end of day: any direction still IN_TRADE (SL never hit) gets its exit5_eod stamped with
        # the day's last close, mirroring the live engine's EOD finalize.
        for direction, ds in self.directions.items():
            if ds.state == STATE_IN_TRADE and ds.current_trade is not None:
                trade = ds.current_trade
                last_ts = str(candle_data.iloc[-1][DATE_TIME])
                last_close = float(candle_data.iloc[-1][CLOSE_PRICE])
                trade.exit5_eod.option_price = self.__historical_option_close_near(ds.option_symbol, last_ts) or 0.0
                trade.exit5_eod.timestamp = last_ts
                trade.mae, trade.mae_time = ds.mae, ds.mae_time
                trade.mfe, trade.mfe_time = ds.mfe, ds.mfe_time
                self.results.append(trade)
                self.__log(f"End of day ({direction}): trade still open (SL not hit), closed at last price "
                          f"{last_close} ({self.__fmt_option_price(trade.exit5_eod.option_price)})")

        return self.results, signal_option_symbol, STATUS_OK

    def __check_reclaim_backtest(self, ds: _DirectionState, row, sub_rows, ts):
        for r in sub_rows:
            if self.__check_reclaim(ds, r, row[VWAP], str(r[DATE_TIME])):
                return
        # Not reclaimed yet -- keep waiting on subsequent candles rather than abandoning after
        # just one miss. The piercing candle stays the reference point.
        self.__log(f"[{ts}] ({ds.direction}) no reclaim yet, still waiting {self.__fmt_candle(row)}")

    def __check_confirm_entry_backtest(self, ds: _DirectionState, row, sub_rows, ts):
        # Walk this main candle's 1-min candles through the shared entry rule (__stop_entry_fill);
        # on entry, the rest of the candle's minutes are already in-trade.
        if ds.piercing_candle is None or ds.reclaim_candle is None:
            return
        row_start = self.__candle_start(row)
        row_end = row_start + timedelta(minutes=self.candle_interval_minutes)
        for bar in self.__minute_bars(row_start, row_end):
            fill = self.__stop_entry_fill(ds, bar)
            if fill is None:
                continue
            bar_ts = str(bar[DATE_TIME])
            self.__log(f"[{bar_ts}] ({ds.direction}) waiting for entry: piercing_high={float(ds.piercing_candle[HIGH_PRICE])} "
                      f"1-min H={bar[HIGH_PRICE]} -> TRIGGERED @ {fill}")
            self.__enter_trade(ds, fill, bar_ts, main_row=row)
            self.__run_in_trade_minutes_backtest(
                ds, self.__minute_bars(self.__candle_start(bar) + timedelta(minutes=1), row_end))
            return
        self.__log(f"[{ts}] ({ds.direction}) waiting for entry: piercing_high={float(ds.piercing_candle[HIGH_PRICE])} "
                  f"{self.__fmt_candle(row)}")

    def __has_acceptable_top_wick(self, row):
        body = abs(float(row[CLOSE_PRICE]) - float(row[OPEN_PRICE]))
        top_wick = float(row[HIGH_PRICE]) - max(float(row[OPEN_PRICE]), float(row[CLOSE_PRICE]))
        return top_wick <= body

    def __check_in_trade_backtest(self, ds: _DirectionState, row, ts):
        # Run this main candle's 1-min candles through the shared in-trade minute logic -- the same
        # per-minute SL/exit/MAE-MFE handling LIVE test mode uses, so both give the same trade.
        row_start = self.__candle_start(row)
        row_end = row_start + timedelta(minutes=self.candle_interval_minutes)
        self.__run_in_trade_minutes_backtest(ds, self.__minute_bars(row_start, row_end))
        if ds.current_trade is None:
            return
        # Exit-4 (Bollinger) is informational only here -- logged, not an exit.
        bollinger_level = self.__get_bollinger_exit_level_backtest(ds)
        bollinger_str = f"{bollinger_level:.2f}" if bollinger_level is not None else "n/a"
        self.__log(f"[{ts}] ({ds.direction}) in-trade {self.__fmt_candle(row)} SL={ds.sl_level} Exit4(Bollinger)={bollinger_str}")

    def __run_in_trade_minutes_backtest(self, ds: _DirectionState, bars):
        for bar in bars:
            outcome = self.__in_trade_minute(ds, bar)
            if outcome is None:
                continue
            trade, bar_ts = ds.current_trade, str(bar[DATE_TIME])
            self.__stamp_mae_mfe(ds)
            self.results.append(trade)
            if outcome == "FORCE":
                self.__log(f"[{bar_ts}] ({ds.direction}) force-exit (14:50 cutoff) "
                          f"({self.__fmt_option_price(trade.exit5_eod.option_price)}) -- trade closed, resuming scan")
            else:
                self.__log(f"[{bar_ts}] ({ds.direction}) SL hit @ {ds.sl_level} "
                          f"({self.__fmt_option_price(trade.sl_hit.option_price)}) -- trade closed, resuming scan")
            self.__reset_direction_backtest(ds)
            return

    def __get_bollinger_exit_level_backtest(self, ds: _DirectionState):
        i = self._backtest_row_index
        band = self.upper_band.iloc[i] if ds.direction == "BUY" else self.lower_band.iloc[i]
        if band != band:  # NaN check -- first BOLLINGER_PERIOD candles have no band yet
            return None
        return float(band)

    def __reset_direction_backtest(self, ds: _DirectionState):
        ds.current_trade = None
        ds.option_symbol = ""
        ds.piercing_candle = None
        ds.reclaim_candle = None
        ds.state = STATE_SEEK_PIERCING

    def __log(self, message):
        self.log_fn(message)

    # ------------------------------------------------------------------
    # shared pattern-transition logic (identical for both modes)
    # ------------------------------------------------------------------
    def __check_abandon_incomplete_setup(self, ds: _DirectionState, ts):
        """
        True (and resets ds) if a setup that pierced but hasn't entered yet (SEEK_RECLAIM /
        SEEK_CONFIRM_ENTRY) is still incomplete at PIERCING_CUTOFF_TIME -- same cutoff as new
        piercing detection, independent of FORCE_EXIT_TIME (which only applies once IN_TRADE).
        """
        if not pattern_rules.is_setup_abandon_time_reached(pattern_rules.time_of_day(ts)):
            return False
        msg = f"[{ts}] ({ds.direction}) giving up on incomplete setup (still {ds.state}) at cutoff -- resetting to seek piercing"
        if self.mode == Mode.LIVE:
            print(self.logic_name, ":", msg)
        else:
            self.__log(msg)
        ds.state = STATE_SEEK_PIERCING
        ds.piercing_candle = None
        ds.reclaim_candle = None
        return True

    def __check_seek_piercing(self, ds: _DirectionState, row, ts):
        window_open = pattern_rules.is_piercing_window_open(pattern_rules.time_of_day(ts), self.piercing_start_time)
        if not window_open:
            # LIVE polls every candle regardless of the window and always reports "seeking";
            # BACKTEST silently skips pre/post-window candles without a "seeking" line -- both
            # match their pre-existing behavior.
            if self.mode == Mode.LIVE:
                print(self.logic_name, f": [{ts}] ({ds.direction}) seeking piercing {self.__fmt_candle(row)}")
            return
        direction = pattern_rules.piercing_direction(row)
        if direction != ds.direction:
            msg = f"[{ts}] ({ds.direction}) seeking piercing {self.__fmt_candle(row)}"
            if self.mode == Mode.LIVE:
                print(self.logic_name, ":", msg)
            else:
                self.__log(msg)
            return
        ds.piercing_candle = row
        ds.state = STATE_SEEK_RECLAIM
        if self.mode == Mode.LIVE:
            # NOTE: LIVE's original wording puts direction after "PIERCING" (unlike its own
            # "seeking piercing" line, and unlike BACKTEST's "(direction) PIERCING" below) --
            # preserved exactly as-is rather than unified, since this task is about sharing
            # logic, not changing existing log wording.
            print(self.logic_name, f": [{ts}] PIERCING ({ds.direction}) {self.__fmt_candle(row)}")
        else:
            self.__log(f"[{ts}] ({ds.direction}) PIERCING {self.__fmt_candle(row)}")

    def __check_reclaim(self, ds: _DirectionState, row, vwap, ts):
        """True (and transitions state) if this row reclaims; caller logs the miss case.

        Reclaim is defined as this row's Close moving back inside the piercing candle's body
        on the opposite side (user-requested behavior): for BUY piercing, reclaim means the
        candle closed below the piercing candle's Close; for SELL piercing, closed above it.
        """
        if ds.piercing_candle is None:
            return False
        piercing_close = float(ds.piercing_candle[CLOSE_PRICE])
        if ds.direction == "BUY":
            reclaimed = float(row[CLOSE_PRICE]) < piercing_close
        else:
            reclaimed = float(row[CLOSE_PRICE]) > piercing_close
        if not reclaimed:
            return False
        ds.reclaim_candle = row
        # keep track of the main-interval VWAP that this reclaim was measured against for history
        ds.reclaim_vwap = vwap
        ds.state = STATE_SEEK_CONFIRM_ENTRY
        msg = f"[{ts}] ({ds.direction}) RECLAIM {self.__fmt_candle(row, vwap)}"
        if self.mode == Mode.LIVE:
            print(self.logic_name, ":", msg)
        else:
            self.__log(msg)
        return True

    def __enter_trade(self, ds: _DirectionState, entry_trigger_price, ts, main_row=None):
        option_symbol, option_price = "", 0.0
        option_type = self.option_type
        if self.mode == Mode.LIVE:
            broker = self.trade_utility.get_broker_utility()
            # Prefer preselected cheapest option from pre_requisite stage; fall back to on-demand selection
            pre_sym, pre_price = self.preselected_options.get(option_type, (None, 0.0))
            if pre_sym:
                option_symbol, option_price = pre_sym, pre_price
            else:
                option_symbol, option_price = self.__select_option_by_premium(broker, option_type)
            if option_symbol is None:
                print(self.logic_name, f": ({ds.direction}) No option found near target premium band; dropping setup")
                ds.state = STATE_SEEK_PIERCING
                ds.piercing_candle = None
                ds.reclaim_candle = None
                return
            if self.test_mode:
                # the traded option is the signal option, so the entry fill is its price
                option_price = entry_trigger_price
            else:
                current_price = self.__ltp(option_symbol)
                if current_price is not None:
                    option_price = current_price
        else:
            # BACKTEST: real historical premiums, unlike the live path, aren't available from a
            # single batched call -- resolve the ~40 candidate strikes around ATM the same way
            # live's option chain would, and check each one's own historical 1-min close near the
            # entry minute individually. Unlike LIVE, a miss here does NOT drop the setup -- the
            # trade still proceeds and is logged, just without option pricing (per requirement:
            # this is a reporting enhancement, not a precondition for the trade existing).
            option_symbol = self.signal_option_symbol
            # the traded option is the signal option, so the entry fill (__stop_entry_fill) is
            # its entry price -- same as LIVE test mode
            option_price = entry_trigger_price
            if option_symbol is None:
                option_symbol, option_price = "", 0.0
                self.__log(f"[{ts}] ({ds.direction}) No option found in "
                          f"{self.target_premium_low:.0f}-{self.target_premium_high:.0f} band at entry -- "
                          f"option price data unavailable for this trade")

        trade = paper_trade_row()
        trade.date = self.session_date_str if self.mode == Mode.LIVE else self.trade_date_str
        trade.option_name = option_symbol
        trade.trade_type = ds.direction
        trade.piercing_candle = self.__to_snapshot(ds.piercing_candle)
        trade.reclaim_candle = self.__to_snapshot(ds.reclaim_candle, ds.reclaim_vwap)
        # BACKTEST and test mode enter on a 1-min candle (__stop_entry_fill), whose confirmation is
        # the latest main-interval candle closed before that minute began -- record that one.
        confirm = None
        if self.mode == Mode.BACKTEST or self.test_mode:
            confirm = self.__main_candle_closed_before(self.__candle_start({DATE_TIME: ts}))
        if confirm is not None:
            trade.confirm_candle = self.__to_snapshot(confirm)
        elif self.mode == Mode.LIVE:
            trade.confirm_candle = candle_snapshot(timestamp=ts, open=entry_trigger_price, high=entry_trigger_price,
                                                   low=entry_trigger_price, close=entry_trigger_price, vwap=0.0)
        else:
            trade.confirm_candle = self.__to_snapshot(main_row)
        trade.entry_trigger_price = entry_trigger_price
        trade.entry_option_price = option_price
        trade.entry_timestamp = ts

        # SL (low of piercing candle) persisted in the trade row for downstream reporting
        piercing_low = float(ds.piercing_candle[LOW_PRICE])
        trade.sl_low = piercing_low

        piercing_high = float(ds.piercing_candle[HIGH_PRICE])
        piercing_low = float(ds.piercing_candle[LOW_PRICE])
        piercing_length = piercing_high - piercing_low

        option_entry_price = option_price
        ds.sl_level = max(0.0, option_entry_price - piercing_length)
        ds.exit1_level = option_entry_price + piercing_length
        ds.exit2_level = option_entry_price + (2 * piercing_length)
        ds.exit3_level = option_entry_price + 7.0
        ds.exit4_10_level = option_entry_price + 10.0
        ds.exit4_15_level = option_entry_price + 15.0
        ds.exit4_20_level = option_entry_price + 20.0
        ds.exit4_30_level = option_entry_price + 30.0
        ds.exit4_40_level = option_entry_price + 40.0

        ds.current_trade = trade
        ds.option_symbol = option_symbol
        ds.mae = 0.0
        ds.mfe = 0.0
        ds.mae_time = ts
        ds.mfe_time = ts
        ds.state = STATE_IN_TRADE

        if self.mode == Mode.LIVE:
            if not self.test_mode:
                self.quotes_utility.add_stocks([option_symbol], [self.__option_market_type()])
            # reset the once-per-candle throttle on entry so the first in-trade status line isn't
            # suppressed by the candle timestamp already logged during the waiting-for-entry phase.
            ds.last_logged_candle_ts = None
            print(self.logic_name, ": LIVE ENTRY", ds.direction, option_symbol, "@", option_price,
                  "entry_trigger=", entry_trigger_price, "ts=", ts)
        else:
            # Exit-4 (Bollinger) isn't fixed at entry like SL/Exit-1..3 -- it moves every candle
            # (checked in __check_in_trade_backtest below). Shown here is just its value at the
            # moment of entry, for visibility.
            entry_bollinger_level = self.__get_bollinger_exit_level_backtest(ds)
            exit4_str = f"{entry_bollinger_level:.2f}" if entry_bollinger_level is not None else "n/a"
            option_str = f"{option_symbol} @{option_price:.2f}" if option_symbol else "none found"
            self.__log(f"[{ts}] ({ds.direction}) CONFIRM/ENTRY @ {entry_trigger_price} "
                      f"(piercing: {ds.piercing_candle[DATE_TIME]}, reclaim: {ds.reclaim_candle[DATE_TIME]}) "
                      f"SL={ds.sl_level} Exit1={ds.exit1_level:.2f} Exit2={ds.exit2_level:.2f} Exit3={ds.exit3_level:.2f} "
                      f"Exit4(@entry)={exit4_str} Option={option_str}")

    def __update_mae_mfe_point(self, ds: _DirectionState, option_ltp, ts):
        # single option-price point excursion from entry.
        excursion = option_ltp - ds.current_trade.entry_option_price
        if excursion < ds.mae:
            ds.mae, ds.mae_time = excursion, ts
        if excursion > ds.mfe:
            ds.mfe, ds.mfe_time = excursion, ts

    def __update_mae_mfe_range(self, ds: _DirectionState, high, low, ts):
        # worst/best excursion across a candle's whole High/Low range, from the entry option price
        # (same base as __update_mae_mfe_point and the exit P&L).
        entry = ds.current_trade.entry_option_price
        worst = (low - entry) if ds.direction == "BUY" else (entry - high)
        best = (high - entry) if ds.direction == "BUY" else (entry - low)
        if worst < ds.mae:
            ds.mae, ds.mae_time = worst, ts
        if best > ds.mfe:
            ds.mfe, ds.mfe_time = best, ts

    def __stamp_mae_mfe(self, ds: _DirectionState):
        ds.current_trade.mae = ds.mae
        ds.current_trade.mae_time = ds.mae_time
        ds.current_trade.mfe = ds.mfe
        ds.current_trade.mfe_time = ds.mfe_time

    def __snapshot_exit_hits(self, trade: paper_trade_row):
        # capture before-state so we can log the exact moment each hypothesis first hits -- none
        # of Exit-1..4 stop the trade (only SL does), so without this there's no visibility into
        # when/whether they fired.
        return {
            "Exit1 (Length of Piercing)": trade.exit1_hit.is_hit,
            "Exit2 (2 x Length of Piercing)": trade.exit2_hit.is_hit,
            "Exit3 (7 Points)": trade.exit3_hit.is_hit,
            "Exit4 (Bollinger)": trade.exit4_hit.is_hit,
        }

    def __fmt_option_price(self, price):
        return f"opt@{price:.2f}" if price and price > 0 else "opt@n/a"

    def __log_exit_breaches(self, ds: _DirectionState, was_hit):
        trade = ds.current_trade
        for label, hit_obj in (("Exit1 (Length of Piercing)", trade.exit1_hit),
                               ("Exit2 (2 x Length of Piercing)", trade.exit2_hit),
                               ("Exit3 (7 Points)", trade.exit3_hit),
                               ("Exit4 (Bollinger)", trade.exit4_hit)):
            if was_hit[label] or not hit_obj.is_hit:
                continue
            opt_str = self.__fmt_option_price(hit_obj.option_price)
            if self.mode == Mode.LIVE:
                print(self.logic_name, f": ({ds.direction}) {label} target BREACHED @ {opt_str}",
                     "(hypothesis only -- trade continues, only SL closes it)")
            else:
                self.__log(f"[{hit_obj.timestamp}] ({ds.direction}) {label} target BREACHED @ {opt_str} "
                          f"(hypothesis only -- trade continues, only SL closes it)")

    # ------------------------------------------------------------------
    # 1-min trade handling shared by BACKTEST and LIVE test mode
    # ------------------------------------------------------------------
    def __candle_start(self, candle):
        # a candle's start as a datetime on the trade date, whatever format the broker uses
        return datetime.strptime(f"{self.trade_date_str} {pattern_rules.time_of_day(str(candle[DATE_TIME]))}",
                                 "%Y-%m-%d %H:%M:%S")

    def __minute_bars(self, start, end):
        # BACKTEST: the 1-min candles starting in [start, end)
        if self.minute_data is None:
            return []
        return [self.minute_data.iloc[k] for k, t in enumerate(self.minute_starts) if start <= t < end]

    def __main_candle_closed_before(self, moment):
        # latest main-interval candle that had fully closed by `moment`
        data = self.last_candle_data
        if data is None or len(data) == 0:
            return None
        for k in range(len(data) - 1, -1, -1):
            if self.__candle_start(data.iloc[k]) + timedelta(minutes=self.candle_interval_minutes) <= moment:
                return data.iloc[k]
        return None

    def __stop_entry_fill(self, ds: _DirectionState, bar):
        """
        Entry rule, identical in BACKTEST and LIVE test mode, checked on one 1-min candle after the
        reclaim candle has closed: if its range reaches the piercing candle's extreme (BUY: high),
        the entry fills at that level like a stop order -- or at the candle's open if it already
        opened beyond it. The confirmation (top wick <= body) is on the latest main-interval
        candle closed before this minute began. Returns the fill price, or None.
        """
        bar_start = self.__candle_start(bar)
        if bar_start < self.__candle_start(ds.reclaim_candle) + timedelta(minutes=5):
            return None
        confirm = self.__main_candle_closed_before(bar_start)
        if confirm is None or not self.__has_acceptable_top_wick(confirm):
            return None
        bar_open = float(bar[OPEN_PRICE])
        if ds.direction == "BUY":
            level = float(ds.piercing_candle[HIGH_PRICE])
            return max(bar_open, level) if float(bar[HIGH_PRICE]) >= level else None
        level = float(ds.piercing_candle[LOW_PRICE])
        return min(bar_open, level) if float(bar[LOW_PRICE]) <= level else None

    def __in_trade_minute(self, ds: _DirectionState, bar):
        """
        One 1-min candle of an open trade, identical in BACKTEST and LIVE test mode: MAE/MFE over
        its High/Low, the 14:50 force-exit, then SL and exits on its Close (Exit-3 on its High).
        Returns "FORCE" or "SL" if the trade closed on this candle, else None -- recording the
        closed trade (results list vs sheet) is up to the caller.
        """
        trade = ds.current_trade
        ts = str(bar[DATE_TIME])
        close, high, low = float(bar[CLOSE_PRICE]), float(bar[HIGH_PRICE]), float(bar[LOW_PRICE])
        self.__update_mae_mfe_range(ds, high, low, ts)
        if pattern_rules.is_force_exit_time_reached(pattern_rules.time_of_day(ts)):
            trade.exit5_eod.option_price = close
            trade.exit5_eod.timestamp = ts
            return "FORCE"
        was_hit = self.__snapshot_exit_hits(trade)
        self.__mark_exit_if_hit_point(trade.sl_hit, ds.sl_level, close, ts, is_stop=True)
        self.__mark_exit_if_hit_point(trade.exit1_hit, ds.exit1_level, close, ts)
        self.__mark_exit_if_hit_point(trade.exit2_hit, ds.exit2_level, close, ts)
        self.__mark_exit3_if_hit_point(ds, max(close, high), ts)
        for hit_obj, level in ((trade.exit4_10_hit, ds.exit4_10_level), (trade.exit4_15_hit, ds.exit4_15_level),
                               (trade.exit4_20_hit, ds.exit4_20_level), (trade.exit4_30_hit, ds.exit4_30_level),
                               (trade.exit4_40_hit, ds.exit4_40_level)):
            self.__mark_exit_if_hit_point(hit_obj, level, close, ts, record_target_price=True)
        self.__log_exit_breaches(ds, was_hit)
        return "SL" if trade.sl_hit.is_hit else None

    def __check_entry_trigger_test(self, ds: _DirectionState):
        # LIVE test mode: the newest closed 1-min candle through the shared entry rule
        if ds.piercing_candle is None or ds.reclaim_candle is None:
            return
        bar = self.__last_closed_bar(self.signal_option_symbol)
        if bar is None or str(bar[DATE_TIME]) == ds.last_bar_ts:
            return
        bar_ts = ds.last_bar_ts = str(bar[DATE_TIME])
        fill = self.__stop_entry_fill(ds, bar)
        piercing_high = float(ds.piercing_candle[HIGH_PRICE])
        if fill is None:
            self.__log_once_per_candle(ds, f": [{bar_ts}] ({ds.direction}) waiting for entry: "
                                           f"1-min H={bar[HIGH_PRICE]} vs piercing_high={piercing_high}")
            return
        print(self.logic_name, f": [{bar_ts}] ({ds.direction}) waiting for entry: 1-min H={bar[HIGH_PRICE]} "
                               f"vs piercing_high={piercing_high} -> TRIGGERED @ {fill}")
        self.__enter_trade(ds, fill, bar_ts)

    def __check_exit_hits_test(self, ds: _DirectionState):
        # LIVE test mode: the newest closed 1-min candle through the shared in-trade logic
        bar = self.__last_closed_bar(ds.option_symbol)
        if bar is None or str(bar[DATE_TIME]) == ds.last_bar_ts:
            return
        bar_ts = ds.last_bar_ts = str(bar[DATE_TIME])
        outcome = self.__in_trade_minute(ds, bar)
        self.__log_once_per_candle(ds, f": [{bar_ts}] ({ds.direction}) in-trade C={bar[CLOSE_PRICE]} "
                                       f"SL={ds.sl_level} Exit1={ds.exit1_level:.2f} Exit2={ds.exit2_level:.2f} "
                                       f"Exit3={ds.exit3_level:.2f} (option-price based)")
        if outcome is not None:
            self.__stamp_mae_mfe(ds)
            self.__finalize_and_reset_live(ds, "Force Exit 14:50" if outcome == "FORCE" else "SL")

    def __to_snapshot(self, row, vwap=None):
        # vwap is an explicit override for 5-min reclaim rows, which carry no VWAP of their own
        # -- they're measured against the main-interval candle's VWAP instead.
        v = row[VWAP] if vwap is None else vwap
        return candle_snapshot(timestamp=str(row[DATE_TIME]), open=float(row[OPEN_PRICE]),
                               high=float(row[HIGH_PRICE]), low=float(row[LOW_PRICE]),
                               close=float(row[CLOSE_PRICE]), vwap=float(v))

    def __fmt_candle(self, row, vwap=None):
        v = row[VWAP] if vwap is None else vwap
        if self.mode == Mode.LIVE:
            return (f"O={row[OPEN_PRICE]} H={row[HIGH_PRICE]} L={row[LOW_PRICE]} C={row[CLOSE_PRICE]} "
                   f"VWAP={v:.2f}")
        # BACKTEST: no intrabar ticks are available historically, so LTP is approximated as this
        # candle's Close -- shown explicitly, unlike the live format above.
        return (f"O={row[OPEN_PRICE]} H={row[HIGH_PRICE]} L={row[LOW_PRICE]} C={row[CLOSE_PRICE]} "
               f"LTP={row[CLOSE_PRICE]} VWAP={v:.2f}")


def _exit_pnl_points(trade: paper_trade_row, hit_obj: exit_hit):
    """Signed profit/loss in option-premium points for a hit exit, relative to entry."""
    if trade.trade_type == "BUY":
        return hit_obj.option_price - trade.entry_option_price
    return trade.entry_option_price - hit_obj.option_price


def determine_best_case_exit(trade: paper_trade_row):
    """
    Which of Exit-1..4 would have been the best-case exit for a finalized trade, i.e. whichever
    hit target represents the largest profit in points. Returns "SL" if none of Exit-1..4 were
    ever hit before the trade closed. BackTestData-only -- not written to PaperTradeData.
    """
    candidates = [(label, _exit_pnl_points(trade, hit_obj))
                 for label, hit_obj in (("Exit1", trade.exit1_hit), ("Exit2", trade.exit2_hit),
                                        ("Exit3", trade.exit3_hit), ("Exit4", trade.exit4_hit))
                 if hit_obj.is_hit]
    if not candidates:
        return "SL"
    best_label, _ = max(candidates, key=lambda c: c[1])
    return best_label


def describe_exit_outcomes(trade: paper_trade_row):
    """
    Full description for the Best Case Exit column: profit/loss in points for every Exit-1..4
    that was hit, plus which one was best, each annotated with the option's own premium at that
    exit -- e.g. "NIFTY2681122050PE entry@105.00 | Exit1:+23.90pts(opt@112.50),
    Exit3:+65.20pts(opt@98.50) (Best: Exit3)". If none of Exit-1..4 were ever hit before the trade
    closed, the exits portion is just "SL". If no option was ever resolved for this trade (e.g.
    backtest couldn't find one in the target premium band), the option prefix is replaced with an
    explicit "[No option data found]" note rather than silently omitted.
    """
    parts = []
    best_label, best_pnl = None, None
    for label, hit_obj in (("Exit1", trade.exit1_hit), ("Exit2", trade.exit2_hit),
                           ("Exit3", trade.exit3_hit), ("Exit4", trade.exit4_hit)):
        if not hit_obj.is_hit:
            continue
        pnl = _exit_pnl_points(trade, hit_obj)
        opt_str = f"(opt@{hit_obj.option_price:.2f})" if hit_obj.option_price > 0 else "(opt@n/a)"
        parts.append(f"{label}:{pnl:+.2f}pts{opt_str}")
        if best_pnl is None or pnl > best_pnl:
            best_label, best_pnl = label, pnl

    exits_desc = f"{', '.join(parts)} (Best: {best_label})" if parts else "SL"

    if trade.option_name:
        return f"{trade.option_name} entry@{trade.entry_option_price:.2f} | {exits_desc}"
    return f"{exits_desc} [No option data found]"
