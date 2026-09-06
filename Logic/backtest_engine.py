# -*- coding: utf-8 -*-
"""
backtest_engine.py

Thin entry point for the historical backtest replay. The actual state
machine (Piercing -> Reclaim -> Confirm/Entry -> SL/Exit-1..4/EOD) lives in
piercing_engine.py's VwapPiercingEngine, shared verbatim with the live
engine (Logic/vwap_piercing_options.py) so a rule change can't require
touching two places and drifting between them -- only this file's job is to
adapt that shared engine's BACKTEST mode to the run_backtest_for_day(...)
function signature run_backtest.py and tests/test_pattern_dry_run.py expect.

Rows are built as paper_trade_row (same dataclass the live engine writes to
PaperTradeData) since BackTestData now uses an identical sheet layout --
option_name/option_price fields are just left blank/0.0 here since there's
no real historical option premium data to fill them with.
"""
from concurrent.futures import ThreadPoolExecutor

from .piercing_engine import (
    VwapPiercingEngine, Mode, STATUS_NO_DATA, STATUS_OK,
    determine_best_case_exit, describe_exit_outcomes, _exit_pnl_points,
)
from .pattern_rules import resolve_front_month_future_symbol


def run_backtest_for_day(broker, index_name, trade_date_str, candle_interval_minutes, log_fn=None):
    """
    Returns (list_of_paper_trade_row, future_symbol, status) for one trading day.
    log_fn, if given, is called with a string for each pattern event (Piercing/Reclaim/
    invalidation/Confirm-Entry/SL-close) -- pass print for a verbose dry-run trace.
    """
    def run_option_engine(option_type):
        engine = VwapPiercingEngine(
            mode=Mode.BACKTEST, option_type=option_type, broker=broker,
            index_name=index_name, trade_date_str=trade_date_str,
            candle_interval_minutes=candle_interval_minutes, log_fn=log_fn)
        return engine.run_backtest_day()

    # CE and PE must be evaluated over the same market window, rather than replaying the
    # entire day for one option type before starting the other.
    with ThreadPoolExecutor(max_workers=2) as executor:
        runs = list(executor.map(run_option_engine, ("CE", "PE")))

    results = [trade for option_results, _, _ in runs for trade in option_results]
    future_symbol = next((symbol for _, symbol, _ in runs if symbol), "")
    statuses = [status for _, _, status in runs]

    if all(status == STATUS_NO_DATA for status in statuses):
        return results, future_symbol, STATUS_NO_DATA
    return results, future_symbol, STATUS_OK
