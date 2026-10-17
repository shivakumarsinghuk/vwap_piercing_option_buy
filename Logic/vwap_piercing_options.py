# -*- coding: utf-8 -*-
"""
vwap_piercing_options.py

Thin entry point for the live paper-trade signal logger. The actual state
machine (Piercing -> Reclaim -> Confirm/Entry -> SL/Exit-1..4/EOD) lives in
piercing_engine.py's VwapPiercingEngine, shared verbatim with the backtest
engine (Logic/backtest_engine.py) so a rule change can't require touching
two places and drifting between them -- only this file's job is to run that
shared engine in LIVE mode with the constructor shape executor.py's
LOGIC_REGISTRY / interfaces.py expect (args, broker_utility_manager,
quotes_utility), started on threads exactly as before.

Each completed trade is logged to the PaperTradeData sheet. Real orders (BUY on
entry, SELL on exit) are sent through the broker utility only when
Config/order_config.json's place_order is true -- otherwise it's paper trade only.
"""
from .piercing_engine import VwapPiercingEngine, Mode


class LogicVwapPiercingOptionsBuy(VwapPiercingEngine):

    def __init__(self, args, broker_utility_manager, quotes_utility, option_type="CE"):
        super().__init__(mode=Mode.LIVE, option_type=option_type, args=args,
                         broker_utility_manager=broker_utility_manager, quotes_utility=quotes_utility)
