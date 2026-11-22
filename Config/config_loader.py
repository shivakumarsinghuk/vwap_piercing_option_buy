# -*- coding: utf-8 -*-
"""
config_loader.py

Loads Config/order_config.json once and hands back a plain dict, with DEFAULTS filled in for any
key the file omits (or for the whole file if it's missing). The defaults keep order placement
OFF, so a missing or partial config file can never start sending real orders by accident.
"""
import json
import math
import os
from datetime import datetime

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ORDER_CONFIG_PATH = os.path.join(_CONFIG_DIR, "order_config.json")
# The webhook URL lets anyone who has it trigger orders on the account, so it lives in its own
# gitignored file instead of order_config.json.
_WEBHOOK_URL_PATH = os.path.join(_CONFIG_DIR, "webhook_url.txt")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

ORDER_DEFAULTS = {
    "place_order": False,
    "place_order_in_test_mode": False,
    "trade_days": ["All"],
    "order_type": "MARKET",
    "entry_stop_limit_offset": 0.5,
    "target_exit": "EXIT1",
    "limit_buffer_percent": 1.0,
    "tick_size": 0.05,
    "product_type": "MIS",
    "exchange": "NSE",
    "lot_size": 65,
    "lot_count": 1,
    "order_route": "api",
    "webhook_payload": {},
}


def normalize_trade_days(value):
    if isinstance(value, str):
        days = value.split(",")
    elif isinstance(value, list):
        days = value
    else:
        raise ValueError("trade_days must be 'All', a weekday, or a list of weekdays")

    normalized_days = []
    for day in days:
        if not isinstance(day, str):
            raise ValueError("trade_days entries must be weekday names or 'All'")
        normalized = day.strip().title()
        if normalized.lower() == "all":
            normalized = "All"
        elif normalized not in WEEKDAYS:
            raise ValueError(
                "trade_days entries must be Monday through Sunday or 'All'")
        if normalized not in normalized_days:
            normalized_days.append(normalized)

    if not normalized_days:
        raise ValueError("trade_days must contain at least one weekday or 'All'")
    if "All" in normalized_days and len(normalized_days) > 1:
        raise ValueError("'All' cannot be combined with individual trade_days")
    return normalized_days


def is_trade_day(trade_date, trade_days):
    normalized_days = normalize_trade_days(trade_days)
    if normalized_days == ["All"]:
        return True
    if isinstance(trade_date, str):
        trade_date = datetime.strptime(trade_date, "%Y-%m-%d").date()
    return trade_date.strftime("%A") in normalized_days


def is_order_placement_enabled(order_config, trade_date, test_mode=False):
    order_key = "place_order_in_test_mode" if test_mode else "place_order"
    return bool(order_config.get(order_key, False)) and is_trade_day(
        trade_date, order_config.get("trade_days", ["All"]))


def load_order_config(path=None):
    """
    ORDER_DEFAULTS with order_config.json's values overlaid ("_comment..." keys dropped), plus
    webhook_url read from Config/webhook_url.txt ("" if that file is missing).
    """
    cfg = dict(ORDER_DEFAULTS)
    config_path = path or _DEFAULT_ORDER_CONFIG_PATH
    if os.path.exists(config_path):
        with open(config_path) as f:
            loaded = json.load(f)
        cfg.update({k: v for k, v in loaded.items() if not k.startswith("_comment")})
    cfg["trade_days"] = normalize_trade_days(cfg["trade_days"])
    cfg["webhook_url"] = ""
    if os.path.exists(_WEBHOOK_URL_PATH):
        with open(_WEBHOOK_URL_PATH) as f:
            cfg["webhook_url"] = f.read().strip()
    try:
        cfg["entry_stop_limit_offset"] = float(cfg["entry_stop_limit_offset"])
        cfg["tick_size"] = float(cfg["tick_size"])
    except (TypeError, ValueError) as error:
        raise ValueError("entry_stop_limit_offset and tick_size must be numeric") from error
    if not math.isfinite(cfg["entry_stop_limit_offset"]) or cfg["entry_stop_limit_offset"] < 0:
        raise ValueError("entry_stop_limit_offset must be a finite, non-negative number")
    if not math.isfinite(cfg["tick_size"]) or cfg["tick_size"] <= 0:
        raise ValueError("tick_size must be a finite, positive number")
    cfg["target_exit"] = str(cfg["target_exit"]).upper()
    if cfg["target_exit"] not in {
        "EXIT1", "EXIT2", "EXIT3", "EXIT4_10", "EXIT4_20", "EXIT4_30", "EXIT4_40",
    }:
        raise ValueError(
            "target_exit must be one of EXIT1, EXIT2, EXIT3, EXIT4_10, EXIT4_20, EXIT4_30, EXIT4_40")
    return cfg
