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

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ORDER_CONFIG_PATH = os.path.join(_CONFIG_DIR, "order_config.json")
# The webhook URL lets anyone who has it trigger orders on the account, so it lives in its own
# gitignored file instead of order_config.json.
_WEBHOOK_URL_PATH = os.path.join(_CONFIG_DIR, "webhook_url.txt")

ORDER_DEFAULTS = {
    "place_order": False,
    "place_order_in_test_mode": False,
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
