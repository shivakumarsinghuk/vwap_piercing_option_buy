# -*- coding: utf-8 -*-
"""
paper_trade.py
"""
import traceback

from Utility.gsheet_utility import *
from ....DataTypes.paper_trade_data import *
from ..backtest.backtest import HEADER_ROWS


class UserInterfacePaperTrade:

    def __init__(self, key):
        self.googlesheet_utility = gsheet_utility(account_file=key,
                                                  spread_sheet_name="VWAP_Piercing_Options_Buy")
        self.gworksheet_paper_trade = self.googlesheet_utility.get_work_sheet("PaperTradeData")
        self.__ensure_header()

    def __ensure_header(self):
        rows = self.gworksheet_paper_trade.get_all_values()
        populated_rows = [
            row for row in rows
            if any(str(cell).strip() for cell in row)
        ]
        if not populated_rows:
            self.gworksheet_paper_trade.update_values(crange="A1", values=HEADER_ROWS, extend=True)
        else:
            self.gworksheet_paper_trade.update_values(crange="A1", values=HEADER_ROWS, extend=True)

    def write_trade(self, p_paper_trade_row: paper_trade_row, p_interval, p_best_case_exit):
        # Interval + Best Case Exit mirror the same two extra columns BackTestData has -- appended
        # at the end, same as UserInterfaceBackTest.write_trade.
        try:
            p_paper_trade_row.interval = p_interval
            p_paper_trade_row.best_case_exit = p_best_case_exit
            values = p_paper_trade_row.to_sheet_row()
            # append_table()'s "find the last table and append after it" heuristic drifts further
            # right on every call once any row's data doesn't start at column A (confirmed in
            # practice -- each botched write becomes the next call's "table", compounding the
            # drift run after run). Find the next empty row explicitly instead, so every row
            # always starts at column A.
            #
            # Scan whole rows, not just column A: HEADER_ROWS' second row is blank in column A
            # (it holds the per-candle "Time Stamp/O/H/L/C/VWAP" sub-labels starting at column D),
            # so a column-A count sees only 1 populated row and sends the first trade of the run
            # to row 2 -- on top of that sub-header. __ensure_header() then rewrites rows 1-2 on
            # the next startup, wiping that trade, which is exactly why live runs left this sheet
            # looking empty while BackTestData (which floors its first data row at 3) kept its
            # rows. Floor at row 3 for the same reason.
            rows = self.gworksheet_paper_trade.get_all_values()
            last_populated_row = max(
                (row_number for row_number, row in enumerate(rows, start=1)
                 if any(str(cell).strip() for cell in row)),
                default=0,
            )
            next_row = max(last_populated_row + 1, 3)
            self.gworksheet_paper_trade.update_values(crange=f"A{next_row}", values=[values], extend=True)
            return True
        except:
            print("Exception while writing paper trade row to PaperTradeData")
            traceback.print_exc()
            return False
