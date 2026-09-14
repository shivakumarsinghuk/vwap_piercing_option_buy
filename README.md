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
