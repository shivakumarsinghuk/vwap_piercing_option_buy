# -*- coding: utf-8 -*-
"""
order_log.py
"""
import threading
import traceback

from Utility.gsheet_utility import *
from ....DataTypes.order_log_data import *


# The CE and PE engines each hold a writer and can place orders at the same moment -- same
# find-next-empty-row race as UserInterfacePaperTrade, so the same lock pattern.
_WRITE_LOCK = threading.Lock()


class UserInterfaceOrderLog:

    def __init__(self, key):
        self.googlesheet_utility = gsheet_utility(account_file=key,
                                                  spread_sheet_name="VWAP_Piercing_Options_Buy")
        self.gworksheet_order_log = self.__get_or_create_sheet("OrderLog")

    def __get_or_create_sheet(self, title):
        # get_work_sheet() returns None for a missing tab -- create it with the header instead
        # of silently dropping every order row.
        with _WRITE_LOCK:
            worksheet = self.googlesheet_utility.get_work_sheet(title)
            if worksheet is None:
                try:
                    worksheet = self.googlesheet_utility.spread_sheet.add_worksheet(title)
                except:
                    # the other engine may have just created it
                    worksheet = self.googlesheet_utility.get_work_sheet(title)
            if worksheet is not None and not any(str(cell).strip() for cell in worksheet.get_row(1)):
                worksheet.update_values(crange="A1", values=[ORDER_LOG_HEADER], extend=True)
        return worksheet

    def write_order(self, p_order_log_row: order_log_row):
        # Same "find the next empty row explicitly" approach as UserInterfacePaperTrade.write_trade
        # -- append_table()'s heuristic drifts once a row doesn't start at column A.
        try:
            values = p_order_log_row.to_sheet_row()
            with _WRITE_LOCK:
                rows = self.gworksheet_order_log.get_all_values()
                last_populated_row = max(
                    (row_number for row_number, row in enumerate(rows, start=1)
                     if any(str(cell).strip() for cell in row)),
                    default=0,
                )
                next_row = max(last_populated_row + 1, 2)
                self.gworksheet_order_log.update_values(crange=f"A{next_row}", values=[values], extend=True)
            return True
        except:
            print("Exception while writing order log row to OrderLog")
            traceback.print_exc()
            return False
