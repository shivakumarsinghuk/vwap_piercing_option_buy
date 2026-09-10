# -*- coding: utf-8 -*-
"""
backtest.py
"""
import traceback

from Utility.gsheet_utility import *
from ....DataTypes.paper_trade_data import *


HEADER_ROWS = [
    [
        "Date", "Option Name", "Trade Type",
        "Piercing Candle", "", "", "", "", "",
        "Reclaim Candle", "", "", "", "", "",
        "Confirm Candle", "", "", "", "", "",
        "Entry", "", "SL (Low of Piercing Candle)", "",
        "Exit-1 (Piercing Candle Length)", "",
        "Exit-2 (Piercing Candle Length)", "",
        "Exit-3 (7 Points)", "",
        "Exit-4 (10 Points)", "",
        "Exit-5(15 Points)", "",
        "Exit-6 (20 Points)", "",
        "Exit-7 (30 Points)", "",
        "Exit-8 (40 Points)", "",
        "Exit-8 (EOD)", "",
        "MAE", "MAE TimeStamp", "MFE", "MFE TimeStamp",
        "Interval", "Best Exit",
    ],
    [
        "", "", "",
        "Time Stamp", "O", "H", "L", "C", "VWAP",
        "Time Stamp", "O", "H", "L", "C", "VWAP",
        "Time Stamp", "O", "H", "L", "C", "VWAP",
        "Time Stamp", "Option Entry Price",
        "Time Stamp", "Option Price",
        "Time Stamp", "Option Price",
        "Time Stamp", "Option Price",
        "Time Stamp", "Option Price",
        "Time Stamp", "Option Price",
        "Time Stamp", "Option Price",
        "Time Stamp", "Option Price",
        "Time Stamp", "Option Price",
        "Time Stamp", "Option Price",
        "Time Stamp", "Option Price",
        "", "", "", "",
        "", "",
    ],
]


class UserInterfaceBackTest:

    def __init__(self, key):
        self.googlesheet_utility = gsheet_utility(account_file=key,
                                                  spread_sheet_name="VWAP_Piercing_Options_Buy")
        self.gworksheet_backtest = self.googlesheet_utility.get_work_sheet("BackTestData")
        self.__ensure_header()

    def __ensure_header(self):
        rows = self.gworksheet_backtest.get_all_values()
        populated_rows = [
            row for row in rows
            if any(str(cell).strip() for cell in row)
        ]
        if not populated_rows:
            self.gworksheet_backtest.update_values(crange="A1", values=HEADER_ROWS, extend=True)
        else:
            self.gworksheet_backtest.update_values(crange="A1", values=HEADER_ROWS, extend=True)

    def write_trade(self, p_trade_row: paper_trade_row, p_interval, p_best_case_exit):
        try:
            p_trade_row.interval = p_interval
            p_trade_row.best_case_exit = p_best_case_exit
            p_trade_row.interval = p_interval
            p_trade_row.best_case_exit = p_best_case_exit
            values = p_trade_row.to_sheet_row()
            rows = self.gworksheet_backtest.get_all_values()
            last_populated_row = max(
                (row_number for row_number, row in enumerate(rows, start=1)
                 if any(str(cell).strip() for cell in row)),
                default=0,
            )
            next_row = max(last_populated_row + 1, 3)
            self.gworksheet_backtest.update_values(crange=f"A{next_row}", values=[values], extend=True)
        except:
            print("Exception while writing backtest row to BackTestData")
            traceback.print_exc()
