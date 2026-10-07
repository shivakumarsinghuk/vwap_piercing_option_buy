# -*- coding: utf-8 -*-
"""
config_loader.py

Loads Config/order_config.json once and hands back a plain dict, with DEFAULTS filled in for any
key the file omits (or for the whole file if it's missing). The defaults keep order placement
OFF, so a missing or partial config file can never start sending real orders by accident.
"""
import json
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
    return cfg
