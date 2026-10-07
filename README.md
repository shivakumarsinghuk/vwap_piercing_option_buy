# vwap_piercing_option_buy

## Run executor.py

Run these commands from the repository root. Replace the `--key` value with
the path to your broker credentials JSON file.

### Run one logic

```bash
python3 executor.py \
	--userinterface gsheet \
	--key ../bankniftyorb-2b0a4e15319b.json \
	--logic vwap_piercing_options_buy
```

The equivalent registry name `vwap_piercing_options` can also be used.

### Run multiple logics

Pass multiple logic names after a single `--logic` option:

```bash
python3 executor.py \
	--userinterface gsheet \
	--key ../bankniftyorb-2b0a4e15319b.json \
	--logic vwap_piercing_options vwap_piercing_options_buy
```

Available logic names are `example`, `vwap_piercing_options`, and
`vwap_piercing_options_buy`.

## Real order placement

Entries and exits can send real orders through the broker utility
(`BrokerUtility`'s `place_order`). This is controlled by
[Config/order_config.json](Config/order_config.json):

| Key | Meaning |
| --- | --- |
| `place_order` | Normal LIVE runs. `false` (default) = paper trade only. `true` = BUY the option on entry and SELL it on exit (SL, 14:50 force exit, end of day, executor stop). |
| `place_order_in_test_mode` | `--test_mode true --date ...` replays only, independent of `place_order`. Default `false`. |
| `order_type` | `MARKET` or `LIMIT`. |
| `limit_buffer_percent`, `tick_size` | `LIMIT` only: BUY at LTP + buffer%, SELL at LTP - buffer%, rounded to the tick size. |
| `product_type` | `MIS` (intraday) or `NRML`. |
| `exchange` | Exchange prefix passed to the broker (`NSE` for Fyers). |
| `lot_size`, `lot_count` | Order quantity = `lot_size * lot_count`. |

Backtests never place orders. Order failures are handled as follows:

- Entry BUY fails: the trade still runs and is logged to PaperTradeData as a
  paper trade, and no SELL is ever sent for it (both modes).
- Exit SELL fails, normal LIVE: the trade stays open and the SELL is retried on
  the next exit check.
- Exit SELL fails, test mode: the failure is ignored and the trade is closed
  and logged as usual.

Every order attempt (placed or failed) is written as one row to the `OrderLog`
tab of the `VWAP_Piercing_Options_Buy` Google sheet, which is created with its
header row if missing. Columns: Time Stamp, Date, Option Name, Option Type,
Order Type (Entry/Exit), Transaction Type (BUY/SELL), Quantity, Price Type
(MARKET/LIMIT), Limit Price, Order ID, Status (PLACED/FAILED), Reason,
Reference Price.

### Webhook route (FYERS Automate)

Set `"order_route": "webhook"` to send each order as a POST to a FYERS Automate
webhook instead of the Fyers API. The URL is read from `Config/webhook_url.txt`
(gitignored: anyone with the URL can trigger orders). The request body is
`webhook_payload` in `order_config.json` with `{placeholders}` filled in. It must
match the message your FYERS Automate strategy expects; the shipped template is
only a placeholder.
