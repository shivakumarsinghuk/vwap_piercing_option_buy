# -*- coding: utf-8 -*-
"""
order_log_data.py
"""
from dataclasses import dataclass


ORDER_LOG_HEADER = ["Time Stamp", "Date", "Option Name", "Option Type", "Order Type", "Transaction Type",
                    "Quantity", "Price Type", "Limit Price", "Order ID", "Status", "Reason", "Reference Price"]


@dataclass
class order_log_row:
    timestamp: str = ""
    date: str = ""
    option_name: str = ""
    option_type: str = ""  # "CE" / "PE"
    order_type: str = ""  # "Entry" / "Exit"
    transaction_type: str = ""  # "BUY" / "SELL" -- the literal broker side sent
    quantity: int = 0
    price_type: str = ""  # "MARKET" / "LIMIT"
    limit_price: float = 0.0  # 0 for MARKET
    order_id: str = ""
    status: str = ""  # "PLACED" / "FAILED"
    reason: str = ""  # "Entry", or the exit reason: SL / Force Exit 14:50 / EOD / Executor Stop
    reference_price: float = 0.0  # option LTP at the signal, not a real fill price

    def to_sheet_row(self):
        return [self.timestamp, self.date, self.option_name, self.option_type, self.order_type,
                self.transaction_type, self.quantity, self.price_type, self.limit_price, self.order_id,
                self.status, self.reason, self.reference_price]
