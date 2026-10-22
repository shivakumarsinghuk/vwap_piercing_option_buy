import unittest

from BusinessLogic.vwap_piercing_option_buy.Logic.option_selection import select_cheapest_in_band


class SelectCheapestInBandTests(unittest.TestCase):
    def test_error_payload_returns_no_selection(self):
        self.assertEqual(select_cheapest_in_band({"s": "error"}, "PE"), (None, 0.0))

    def test_missing_chain_returns_no_selection(self):
        self.assertEqual(select_cheapest_in_band(None, "CE"), (None, 0.0))


if __name__ == "__main__":
    unittest.main()
