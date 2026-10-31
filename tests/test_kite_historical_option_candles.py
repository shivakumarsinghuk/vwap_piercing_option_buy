# Run: python -m BusinessLogic.vwap_piercing_option_buy.tests.test_kite_historical_option_candles --key <path> [--date YYYY-MM-DD] [--strike 24500]
# -*- coding: utf-8 -*-
"""Read-only diagnostic for Kite historical NIFTY option candles."""
import argparse
from datetime import datetime

from BusinessLogic.vwap_piercing_option_buy.UserInterface.gsheet.login.login import UserInterfaceLogin
from BrokerUtility.pal.utility_manager import utility_manager
from Utility.utility import generate_monthly_expiry_dates, generate_weekly_expiry_dates


def main():
    parser = argparse.ArgumentParser(description="Verify historical Kite option candles")
    parser.add_argument("--key", required=True, help="Path to the Google service-account JSON")
    parser.add_argument("--date", default="2026-10-22", help="Trade date, YYYY-MM-DD")
    parser.add_argument("--strike", type=int, default=24500, help="NIFTY strike to check")
    parser.add_argument("--interval", default="minute", choices=("minute", "3minute", "5minute", "15minute"))
    args = parser.parse_args()

    trade_date = datetime.strptime(args.date, "%Y-%m-%d")
    expiry = generate_weekly_expiry_dates(trade_date, 1)[0]
    is_month_expiry = expiry in generate_monthly_expiry_dates(trade_date, 1)

    login = UserInterfaceLogin(args.key).get_data()
    broker = utility_manager(manual_kite_login=True).get_utility_object(login).get_broker_utility()
    print("Trade date:", args.date, "expiry:", expiry, "strike:", args.strike)

    for option_type in ("CE", "PE"):
        symbol = broker.get_option_name("NIFTY", expiry, is_month_expiry, str(args.strike), option_type)
        token = broker._kite_utitlity__instrumentLookup(symbol)
        print(f"\n{option_type}: {symbol} | instrument token: {token}")
        if token == -1:
            print("Instrument not found in Kite NSE/NFO instrument dumps")
            continue

        start = f"{args.date} 09:15:00"
        end = f"{args.date} 15:30:00"
        try:
            candles = broker.kite.historical_data(token, start, end, args.interval)
        except Exception as error:
            print("Historical API error:", type(error).__name__, str(error))
            continue

        print("Candle count:", len(candles))
        if candles:
            print("First candle:", candles[0])
            print("Last candle:", candles[-1])
            prices = [candle.get("close") for candle in candles if candle.get("close") is not None]
            if prices:
                in_band = [price for price in prices if 80 <= price <= 130]
                print("Close range:", min(prices), "to", max(prices), "; closes in 80-130 band:", len(in_band))


if __name__ == "__main__":
    main()