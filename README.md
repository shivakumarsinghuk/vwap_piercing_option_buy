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
| `order_type` | `MARKET` or `LIMIT` for ordinary entry/exit orders. The reclaim stop-entry uses `SL` (stop-limit). |
| `limit_buffer_percent`, `tick_size` | `LIMIT` only: BUY at LTP + buffer%, SELL at LTP - buffer%, rounded to the tick size. Also rounds stop-limit prices to the exchange tick. |
| `entry_stop_limit_offset` | Price points added to the piercing-candle-high trigger for the stop-limit BUY placed after reclaim. Default `0.5`. |
| `target_exit` | The resting SELL limit target placed after a real entry fills. Supported values: `EXIT1` (piercing length, default), `EXIT2` (2× piercing length), `EXIT3` (+7 points), and `EXIT4_10`, `EXIT4_20`, `EXIT4_30`, `EXIT4_40` (+10/+20/+30/+40 points). |
| `product_type` | `MIS` (intraday) or `NRML`. |
| `exchange` | Exchange prefix passed to the broker (`NSE` for Fyers). |
| `lot_size`, `lot_count` | Order quantity = `lot_size * lot_count`. |

Backtests never place orders. Order failures are handled as follows:

- An ordinary LIVE entry BUY fails: the trade remains paper-tracked and no SELL
  is sent for it. If the reclaim stop-entry cannot be submitted, the incomplete
  setup is reset rather than falling back to an unprotected market BUY.
- Exit SELL fails, normal LIVE: the trade stays open and the SELL is retried on
  the next exit check.
- Exit SELL fails, test mode: the failure is ignored and the trade is closed
  and logged as usual.

For a test-mode replay, `--test-option-symbol` can pin one contract for its option
type and prevent historical premium-band reselection for that type. For example,
`--test-option-symbol NIFTY26N0924550CE` pins that CE contract; the other option
type continues to use normal historical premium-band selection. This flag is
valid only with `--test_mode true`.

In normal LIVE mode with order placement enabled, a confirmed reclaim submits a
broker-side BUY stop-limit order immediately: trigger price is the piercing
candle's High, and limit price is that High plus `entry_stop_limit_offset`.
The strategy waits for the broker's fill status before managing the trade,
uses the reported filled quantity, and cancels an unfilled order when the
setup expires. This stop-entry flow requires the broker API route; webhook
orders are not used because the engine must poll the broker order status.

After the entry fills, the strategy places a broker-side SELL LIMIT for the
configured `target_exit`. If the option price reaches the strategy's stop-loss
level first, it modifies that existing target order to MARKET and waits for
the broker's fill status before closing the tracked position. A rejected or
failed exit remains tracked and is retried rather than being treated as closed.
The target selector supports fixed target levels; the moving Bollinger target
is not available as a resting exit order.

Backtests and LIVE test-mode replays simulate the same configured target using
1-minute option candles: target is considered filled when the minute High reaches
it, and the SL triggers when the minute Low reaches it. If both are touched in
one minute, SL is assumed first because OHLC data cannot establish the order.
Target fills are modeled at the target, or the opening price if the candle gaps
above it; SL market fills are approximated at the stop, or the opening price if
the candle gaps below it. These are historical fill assumptions, not exact
broker execution prices.

Every order attempt (placed or failed) is written as one row to the `OrderLog`
tab of the `VWAP_Piercing_Options_Buy` Google sheet, which is created with its
header row if missing. Columns: Time Stamp, Date, Option Name, Option Type,
Order Type (Entry/Exit), Transaction Type (BUY/SELL), Quantity, Price Type
(MARKET/LIMIT/SL), Limit Price, Order ID, Status (PLACED/FAILED), Reason,
Reference Price.

### Webhook route (FYERS Automate)

Set `"order_route": "webhook"` to send each order as a POST to a FYERS Automate
webhook instead of the Fyers API. The URL is read from `Config/webhook_url.txt`
(gitignored: anyone with the URL can trigger orders). The request body is
`webhook_payload` in `order_config.json` with `{placeholders}` filled in. It must
match the message your FYERS Automate strategy expects; the shipped template is
only a placeholder.
