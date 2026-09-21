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

    def __init__(self):
        self.broker = None
        self.obj_logic = None

    def create(self, args, broker_utility_manager:utility_manager, quotes_utility:QuoteUtility):
        print("Creating VWAP Piercing Options Logic Object")
        self.obj_logic: LogicVwapPiercingOptionsBuy = LogicVwapPiercingOptionsBuy(args, broker_utility_manager, quotes_utility)

    def wait_for_completion(self):
        print("Wait For Completion", self.obj_logic.__class__.__name__)
        if self.obj_logic:
            print("Before Joining thread")
            self.obj_logic.get_thread_info().join()
            print("After Joining thread")

    def force_close_open_trade(self):
        if not self.obj_logic:
            return
        print("FORCE_CLOSE_OPEN_TRADE: checking live trade state before executor exit")
        for ds in self.obj_logic.directions.values():
            if ds.state == self.obj_logic.STATE_IN_TRADE and ds.current_trade is not None:
                print("FORCE_CLOSE_OPEN_TRADE: closing live trade", ds.direction, ds.option_symbol, ds.current_trade.entry_timestamp)
                self.obj_logic._VwapPiercingEngine__finalize_trade_at_eod_live(ds, "Executor Stop")
                print("FORCE_CLOSE_OPEN_TRADE: write attempted for", ds.direction, ds.option_symbol)

    def get_broker_utility(self):
        return self.obj_logic.get_broker_utility()
