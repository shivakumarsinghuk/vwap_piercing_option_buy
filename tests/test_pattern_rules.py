import unittest

from DataTypes.defines import CLOSE_PRICE, HIGH_PRICE, OPEN_PRICE, VWAP
from BusinessLogic.vwap_piercing_option_buy.Logic.pattern_rules import piercing_direction


class PiercingDirectionTests(unittest.TestCase):
    def candle(self, open_price, close_price, high_price, vwap=100):
        return {
            OPEN_PRICE: open_price,
            CLOSE_PRICE: close_price,
            HIGH_PRICE: high_price,
            VWAP: vwap,
        }

    def test_accepts_buy_piercing_with_both_gaps_over_one_point(self):
        row = self.candle(open_price=98, close_price=103, high_price=104)
        self.assertEqual(piercing_direction(row), "BUY")

    def test_accepts_sell_piercing_with_both_gaps_over_one_point(self):
        row = self.candle(open_price=102, close_price=97, high_price=103)
        self.assertEqual(piercing_direction(row), "SELL")

    def test_rejects_open_gap_of_exactly_one_point(self):
        row = self.candle(open_price=99, close_price=103, high_price=104)
        self.assertIsNone(piercing_direction(row))

    def test_rejects_close_gap_of_exactly_one_point(self):
        row = self.candle(open_price=98, close_price=101, high_price=102)
        self.assertIsNone(piercing_direction(row))

    def test_rejects_when_either_gap_is_below_one_point(self):
        row = self.candle(open_price=99.5, close_price=103, high_price=104)
        self.assertIsNone(piercing_direction(row))


if __name__ == "__main__":
    unittest.main()