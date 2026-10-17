# -*- coding: utf-8 -*-
"""
interfaces.py
"""
from .Logic.vwap_piercing_options import *
from .UserInterface.adapter import *
from ..interfaces.ILogic_Interface import *
from BrokerUtility.pal.utility_manager import *
from Utility.quotes_utility import *


class LogicVwapPiercingOptionsInterface(ILogicInterface):

    # CE and PE are traded by two independent engines, exactly as the backtest does
    # (Logic/backtest_engine.py runs one per option type) -- a single engine only ever trades
    # its own option_type, so running just one would silently skip every PE (or CE) setup.
    OPTION_TYPES = ("CE", "PE")

    def __init__(self):
        self.broker = None
        self.obj_logics = []

    def create(self, args, broker_utility_manager:utility_manager, quotes_utility:QuoteUtility):
        print("Creating VWAP Piercing Options Buy Logic Objects:", ", ".join(self.OPTION_TYPES))
        self.obj_logics = [LogicVwapPiercingOptionsBuy(args, broker_utility_manager, quotes_utility, option_type)
                           for option_type in self.OPTION_TYPES]

    def wait_for_completion(self):
        for obj_logic in self.obj_logics:
            print("Wait For Completion", obj_logic.logic_name)
            print("Before Joining thread")
            obj_logic.get_thread_info().join()
            print("After Joining thread")

    def force_close_open_trade(self):
        print("FORCE_CLOSE_OPEN_TRADE: checking live trade state before executor exit")
        for obj_logic in self.obj_logics:
            for ds in obj_logic.directions.values():
                if ds.state == obj_logic.STATE_IN_TRADE and ds.current_trade is not None:
                    print("FORCE_CLOSE_OPEN_TRADE: closing live trade", obj_logic.logic_name, ds.direction, ds.option_symbol, ds.current_trade.entry_timestamp)
                    obj_logic._VwapPiercingEngine__finalize_trade_at_eod_live(ds, "Executor Stop")
                    print("FORCE_CLOSE_OPEN_TRADE: write attempted for", obj_logic.logic_name, ds.direction, ds.option_symbol)

    def get_broker_utility(self):
        return self.obj_logics[0].get_broker_utility()
