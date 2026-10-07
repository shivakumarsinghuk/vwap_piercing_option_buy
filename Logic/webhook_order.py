# -*- coding: utf-8 -*-
"""
webhook_order.py

Sends an order as an HTTP POST to a webhook (FYERS Automate's trigger URL) instead of through the
broker utility's Fyers API. The message body comes from Config/order_config.json's
webhook_payload template, with {placeholders} filled in per order -- the template must match what
the FYERS Automate strategy behind the URL expects.

Placeholders: symbol (bare, e.g. NIFTY26OCT25000CE), fyers_symbol (NSE:NIFTY26OCT25000CE),
transaction_type (BUY/SELL), side (1/-1), quantity, order_type, product, price, reference_price,
option_type, reason. A string that is exactly one placeholder (e.g. "{quantity}") keeps the value's
own type (number stays a number); anything else is formatted as text.
"""
import traceback

import requests

WEBHOOK_TIMEOUT_SECONDS = 10


def render_payload(template, values):
    if isinstance(template, dict):
        return {k: render_payload(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render_payload(v, values) for v in template]
    if isinstance(template, str):
        if template.startswith("{") and template.endswith("}") and template[1:-1] in values:
            return values[template[1:-1]]
        return template.format_map(values)
    return template


def send_webhook_order(url, payload_template, values):
    """
    POSTs the rendered payload. Returns (success, order_id): success means HTTP 2xx and no
    {"s": "error"} body. A webhook may not return a broker order id -- then order_id is a
    "WEBHOOK-<http status>" marker so OrderLog still shows the call went through.
    """
    if not url:
        print("WEBHOOK ORDER: no webhook URL configured (Config/webhook_url.txt)")
        return False, ""
    try:
        payload = render_payload(payload_template, values)
        print("Webhook Request: ", payload)
        response = requests.post(url, json=payload, timeout=WEBHOOK_TIMEOUT_SECONDS)
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text}
        print("Webhook Response: ", response.status_code, body)
        if not response.ok or (isinstance(body, dict) and body.get("s") == "error"):
            return False, ""
        order_id = (body.get("id") or body.get("order_id")) if isinstance(body, dict) else None
        return True, str(order_id or f"WEBHOOK-{response.status_code}")
    except Exception:
        print("Exception while sending webhook order")
        traceback.print_exc()
        return False, ""
