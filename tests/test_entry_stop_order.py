import unittest
from datetime import datetime, timedelta
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from BrokerUtility.broker_platform.fyers.fyers_utility import fyers_utitlity
from BrokerUtility.broker_platform.kite.kite_utility import kite_utitlity
from BusinessLogic.vwap_piercing_option_buy.Config.config_loader import load_order_config
from BusinessLogic.vwap_piercing_option_buy.DataTypes.paper_trade_data import paper_trade_row
from BusinessLogic.vwap_piercing_option_buy.Logic.piercing_engine import (
    Mode,
    STATE_IN_TRADE,
    STATE_SEEK_CONFIRM_ENTRY,
    VwapPiercingEngine,
    _DirectionState,
)
from DataTypes.defines import CLOSE_PRICE, DATE_TIME, HIGH_PRICE, LOW_PRICE, OPEN_PRICE, VWAP


class EntryStopOrderTests(unittest.TestCase):
    def setUp(self):
        self.engine = object.__new__(VwapPiercingEngine)
        self.engine.order_cfg = {
            "entry_stop_limit_offset": 0.5,
            "tick_size": 0.05,
        }
        self.engine.test_option_symbol = None

    def test_backtest_engine_loads_exit_config_without_enabling_orders(self):
        with patch(
                "BusinessLogic.vwap_piercing_option_buy.Logic.piercing_engine.load_order_config",
                return_value={"target_exit": "EXIT1", "place_order": True}):
            engine = VwapPiercingEngine(
                mode=Mode.BACKTEST,
                broker=Mock(),
                index_name="NIFTY",
                trade_date_str="2026-11-11",
                candle_interval_minutes=5,
            )

        self.assertEqual(engine.order_cfg["target_exit"], "EXIT1")
        self.assertFalse(engine.place_orders)
        direction = engine.directions["BUY"]
        direction.current_trade = paper_trade_row()
        target_name, _ = engine._VwapPiercingEngine__configured_target_exit(direction)
        self.assertEqual(target_name, "EXIT1")

    def test_limit_price_adds_configured_offset_to_piercing_high(self):
        price = self.engine._VwapPiercingEngine__entry_stop_limit_price(24399.9)
        self.assertEqual(price, 24400.4)

    def test_limit_price_rounds_up_to_exchange_tick(self):
        self.engine.order_cfg["entry_stop_limit_offset"] = 0.51
        price = self.engine._VwapPiercingEngine__entry_stop_limit_price(100.0)
        self.assertEqual(price, 100.55)

    def test_negative_offset_is_rejected(self):
        self.engine.order_cfg["entry_stop_limit_offset"] = -0.5
        with self.assertRaisesRegex(ValueError, "must be non-negative"):
            self.engine._VwapPiercingEngine__entry_stop_limit_price(100.0)

    def test_order_config_defaults_offset_and_rejects_negative_value(self):
        config = load_order_config(path="/no/such/order_config.json")
        self.assertEqual(config["entry_stop_limit_offset"], 0.5)
        self.assertEqual(config["target_exit"], "EXIT1")

        with NamedTemporaryFile(mode="w", suffix=".json") as config_file:
            config_file.write('{"entry_stop_limit_offset": -0.5}')
            config_file.flush()
            with self.assertRaisesRegex(ValueError, "non-negative"):
                load_order_config(path=config_file.name)

        with NamedTemporaryFile(mode="w", suffix=".json") as config_file:
            config_file.write('{"target_exit": "BOLLINGER"}')
            config_file.flush()
            with self.assertRaisesRegex(ValueError, "target_exit must be one of"):
                load_order_config(path=config_file.name)

    def test_stop_limit_submission_passes_trigger_and_limit_to_broker(self):
        broker = Mock()
        broker.OPTION_MARKET_TYPE = "OPT"
        broker.place_order.return_value = "entry-123"
        self.engine.mode = Mode.LIVE
        self.engine.trade_utility = SimpleNamespace(get_broker_utility=lambda: broker)
        self.engine.order_cfg = {
            "order_type": "MARKET",
            "limit_buffer_percent": 1.0,
            "tick_size": 0.05,
            "product_type": "MIS",
            "exchange": "NSE",
            "order_route": "api",
        }
        self.engine.option_type = "CE"
        self.engine.logic_name = "test"
        self.engine.test_mode = False
        self.engine.session_date_str = "2026-11-05"
        self.engine.obj_order_log_writer = Mock()

        placed, order_id = self.engine._VwapPiercingEngine__place_real_order(
            "NIFTY-CE", "BUY", 65, 24399.9, "Reclaim stop-entry",
            order_type="SL", trigger_price=24399.9, price_override=24400.4,
            return_order_id=True)

        self.assertTrue(placed)
        self.assertEqual(order_id, "entry-123")
        broker.place_order.assert_called_once_with(
            tradingsymbol="NIFTY-CE",
            transaction_type="BUY",
            quantity=65,
            product="MIS",
            order_type="SL",
            price=24400.4,
            trigger_price=24399.9,
            exchange="NSE",
            market_type="OPT",
        )

    def test_confirmed_reclaim_submits_stop_limit_immediately(self):
        broker = Mock()
        broker.OPTION_MARKET_TYPE = "OPT"
        broker.place_order.return_value = "entry-123"
        self.engine.mode = Mode.LIVE
        self.engine.test_mode = False
        self.engine.place_orders = True
        self.engine.logic_name = "test"
        self.engine.option_type = "CE"
        self.engine.signal_option_symbol = "NIFTY-CE"
        self.engine.order_cfg = {
            "entry_stop_limit_offset": 0.5,
            "tick_size": 0.05,
            "order_type": "MARKET",
            "limit_buffer_percent": 1.0,
            "product_type": "MIS",
            "exchange": "NSE",
            "order_route": "api",
            "lot_size": 65,
            "lot_count": 1,
        }
        self.engine.trade_utility = SimpleNamespace(get_broker_utility=lambda: broker)
        self.engine.obj_order_log_writer = Mock()
        self.engine.session_date_str = "2026-11-05"
        ds = _DirectionState("BUY")
        ds.piercing_candle = {
            OPEN_PRICE: 99.0, HIGH_PRICE: 105.0, LOW_PRICE: 98.0,
            CLOSE_PRICE: 104.0, VWAP: 100.0,
        }
        reclaim = {
            OPEN_PRICE: 103.0, HIGH_PRICE: 104.0, LOW_PRICE: 101.0,
            CLOSE_PRICE: 103.0, VWAP: 100.0,
        }

        did_reclaim = self.engine._VwapPiercingEngine__check_reclaim(
            ds, reclaim, 100.0, "2026-11-05 09:40:00")

        self.assertTrue(did_reclaim)
        self.assertEqual(ds.state, STATE_SEEK_CONFIRM_ENTRY)
        self.assertEqual(ds.entry_order_id, "entry-123")
        self.assertTrue(ds.entry_order_required)
        broker.place_order.assert_called_once_with(
            tradingsymbol="NIFTY-CE",
            transaction_type="BUY",
            quantity=65,
            product="MIS",
            order_type="SL",
            price=105.5,
            trigger_price=105.0,
            exchange="NSE",
            market_type="OPT",
        )

    def test_pending_order_fill_uses_broker_fill_price_and_quantity(self):
        broker = Mock()
        broker.getOrderInfoByOrderId.return_value = SimpleNamespace(
            status="Complete", filled_quantity=30, average_price=24400.25, price=24400.4)
        self.engine.trade_utility = SimpleNamespace(get_broker_utility=lambda: broker)
        self.engine.order_cfg = {"lot_size": 65, "lot_count": 1}
        self.engine._VwapPiercingEngine__now = Mock(return_value=datetime(2026, 11, 5, 9, 45))
        self.engine._VwapPiercingEngine__enter_trade = Mock()
        ds = _DirectionState("BUY")
        ds.entry_order_id = "entry-123"
        ds.entry_order_symbol = "NIFTY-CE"
        ds.entry_order_required = True
        ds.entry_order_limit_price = 24400.4

        self.engine._VwapPiercingEngine__check_pending_entry_order(ds)

        self.engine._VwapPiercingEngine__enter_trade.assert_called_once_with(
            ds, 24400.25, "09:45:00", entry_order_already_placed=True,
            filled_quantity=30, entry_order_symbol="NIFTY-CE")
        self.assertEqual(ds.entry_order_id, "")
        self.assertEqual(ds.entry_order_symbol, "")
        self.assertFalse(ds.entry_order_required)

    def test_opened_stop_entry_uses_actual_fill_price_not_current_quote(self):
        self.engine.mode = Mode.LIVE
        self.engine.test_mode = True
        self.engine.option_type = "CE"
        self.engine.logic_name = "test"
        self.engine.session_date_str = "2026-11-05"
        self.engine.trade_date_str = "2026-11-05"
        self.engine.last_candle_data = None
        ltp = Mock(return_value=24450.0)
        self.engine._VwapPiercingEngine__ltp = ltp
        ds = _DirectionState("BUY")
        ds.piercing_candle = {
            DATE_TIME: "2026-11-05 09:30:00", OPEN_PRICE: 99.0, HIGH_PRICE: 105.0,
            LOW_PRICE: 98.0, CLOSE_PRICE: 104.0, VWAP: 100.0,
        }
        ds.reclaim_candle = {
            DATE_TIME: "2026-11-05 09:35:00", OPEN_PRICE: 103.0, HIGH_PRICE: 104.0,
            LOW_PRICE: 101.0, CLOSE_PRICE: 103.0, VWAP: 100.0,
        }
        ds.reclaim_vwap = 100.0

        self.engine._VwapPiercingEngine__enter_trade(
            ds, 24400.25, "09:45:00", entry_order_already_placed=True,
            filled_quantity=30, entry_order_symbol="NIFTY-CE")

        self.assertEqual(ds.state, STATE_IN_TRADE)
        self.assertEqual(ds.order_quantity, 30)
        self.assertEqual(ds.current_trade.entry_option_price, 24400.25)
        ltp.assert_not_called()

    def test_target_order_uses_configured_exit1_level(self):
        self.engine.place_orders = True
        self.engine.test_mode = False
        self.engine.logic_name = "test"
        self.engine.order_cfg = {"target_exit": "EXIT1", "tick_size": 0.05}
        place_order = Mock(return_value=(True, "exit-123"))
        self.engine._VwapPiercingEngine__place_real_order = place_order
        ds = _DirectionState("BUY")
        ds.current_trade = paper_trade_row()
        ds.option_symbol = "NIFTY-CE"
        ds.order_quantity = 30
        ds.exit1_level = 110.0

        placed = self.engine._VwapPiercingEngine__place_target_exit_order(ds)

        self.assertTrue(placed)
        self.assertEqual(ds.exit_order_id, "exit-123")
        self.assertEqual(ds.exit_order_limit_price, 110.0)
        place_order.assert_called_once_with(
            "NIFTY-CE", "SELL", 30, 110.0, "Target EXIT1", order_type="LIMIT",
            price_override=110.0, return_order_id=True)

    def test_stop_loss_modifies_pending_target_order_to_market(self):
        broker = Mock()
        broker.getOrderInfoByOrderId.return_value = SimpleNamespace(
            status="Open", filled_quantity=0)
        broker.modify_order.return_value = "exit-123"
        self.engine.trade_utility = SimpleNamespace(get_broker_utility=lambda: broker)
        self.engine.logic_name = "test"
        self.engine.option_type = "CE"
        self.engine.obj_order_log_writer = Mock()
        ds = _DirectionState("BUY")
        ds.current_trade = paper_trade_row()
        ds.order_quantity = 30
        ds.exit_order_id = "exit-123"
        ds.exit_order_quantity = 30
        ds.exit_order_limit_price = 110.0
        ds.exit_order_reason = "Target"

        modified = self.engine._VwapPiercingEngine__request_market_exit(ds, "SL")

        self.assertTrue(modified)
        self.assertTrue(ds.exit_order_is_market)
        self.assertEqual(ds.exit_order_reason, "SL")
        broker.modify_order.assert_called_once_with(
            order_id="exit-123", order_type="MARKET", price=0, trigger_price=0)

    def test_filled_target_order_is_tracked_without_duplicate_sell(self):
        broker = Mock()
        broker.getOrderInfoByOrderId.return_value = SimpleNamespace(
            status="Complete", filled_quantity=30, average_price=110.0, price=110.0)
        self.engine.trade_utility = SimpleNamespace(get_broker_utility=lambda: broker)
        self.engine._VwapPiercingEngine__now = Mock(return_value=datetime(2026, 11, 5, 10, 0))
        record_fill = Mock()
        self.engine._VwapPiercingEngine__record_exit_order_fill = record_fill
        ds = _DirectionState("BUY")
        ds.current_trade = paper_trade_row()
        ds.order_quantity = 30
        ds.exit_order_id = "exit-123"
        ds.exit_order_quantity = 30
        ds.exit_order_reason = "Target"

        self.engine._VwapPiercingEngine__check_pending_exit_order(ds)

        self.assertEqual(ds.order_quantity, 0)
        record_fill.assert_called_once_with(ds, broker.getOrderInfoByOrderId.return_value)

    def _minute_exit_case(self, open_price, high, low, close):
        self.engine.order_cfg = {"target_exit": "EXIT1"}
        self.engine.mode = Mode.BACKTEST
        self.engine._VwapPiercingEngine__update_mae_mfe_range = Mock()
        self.engine._VwapPiercingEngine__snapshot_exit_hits = Mock(return_value={})
        ds = _DirectionState("BUY")
        ds.current_trade = paper_trade_row()
        ds.current_trade.entry_option_price = 100.0
        ds.sl_level = 90.0
        ds.exit1_level = 110.0
        bar = {
            DATE_TIME: "2026-11-05 10:00:00",
            OPEN_PRICE: open_price,
            HIGH_PRICE: high,
            LOW_PRICE: low,
            CLOSE_PRICE: close,
        }
        outcome = self.engine._VwapPiercingEngine__in_trade_minute(ds, bar)
        return ds, outcome

    def test_backtest_closes_at_configured_target_when_minute_high_reaches_it(self):
        ds, outcome = self._minute_exit_case(open_price=105.0, high=111.0, low=100.0, close=108.0)

        self.assertEqual(outcome, "TARGET")
        self.assertTrue(ds.current_trade.exit1_hit.is_hit)
        self.assertEqual(ds.current_trade.exit1_hit.option_price, 110.0)
        self.assertFalse(ds.current_trade.sl_hit.is_hit)

    def test_fixed_point_targets_are_recorded_before_configured_target_closes_trade(self):
        self.engine.order_cfg = {"target_exit": "EXIT1"}
        self.engine.mode = Mode.BACKTEST
        self.engine._VwapPiercingEngine__update_mae_mfe_range = Mock()
        ds = _DirectionState("BUY")
        ds.current_trade = paper_trade_row()
        ds.current_trade.entry_option_price = 132.0
        ds.sl_level = 100.0
        ds.exit1_level = 154.25
        ds.exit4_10_level = 142.0
        ds.exit4_20_level = 152.0
        bar = {
            DATE_TIME: "2026-11-05 11:31:00",
            OPEN_PRICE: 132.0,
            HIGH_PRICE: 154.25,
            LOW_PRICE: 130.0,
            CLOSE_PRICE: 135.0,
        }

        outcome = self.engine._VwapPiercingEngine__in_trade_minute(ds, bar)

        self.assertEqual(outcome, "TARGET")
        self.assertTrue(ds.current_trade.exit1_hit.is_hit)
        self.assertTrue(ds.current_trade.exit4_10_hit.is_hit)
        self.assertEqual(ds.current_trade.exit4_10_hit.timestamp, bar[DATE_TIME])
        self.assertEqual(ds.current_trade.exit4_10_hit.option_price, 142.0)
        self.assertTrue(ds.current_trade.exit4_20_hit.is_hit)
        self.assertEqual(ds.current_trade.exit4_20_hit.timestamp, bar[DATE_TIME])
        self.assertEqual(ds.current_trade.exit4_20_hit.option_price, 152.0)

    def test_backtest_uses_open_price_for_target_gap(self):
        ds, outcome = self._minute_exit_case(open_price=112.0, high=115.0, low=111.0, close=113.0)

        self.assertEqual(outcome, "TARGET")
        self.assertEqual(ds.current_trade.exit1_hit.option_price, 112.0)

    def test_backtest_assumes_stop_first_when_minute_touches_both_levels(self):
        ds, outcome = self._minute_exit_case(open_price=100.0, high=112.0, low=89.0, close=105.0)

        self.assertEqual(outcome, "SL")
        self.assertTrue(ds.current_trade.sl_hit.is_hit)
        self.assertEqual(ds.current_trade.sl_hit.option_price, 90.0)
        self.assertFalse(ds.current_trade.exit1_hit.is_hit)

    def test_backtest_uses_open_price_for_gap_through_stop(self):
        ds, outcome = self._minute_exit_case(open_price=87.0, high=89.0, low=85.0, close=86.0)

        self.assertEqual(outcome, "SL")
        self.assertEqual(ds.current_trade.sl_hit.option_price, 87.0)

    def test_backtest_records_and_resets_trade_on_target(self):
        ds = _DirectionState("BUY")
        ds.current_trade = paper_trade_row()
        ds.current_trade.entry_option_price = 100.0
        ds.sl_level = 90.0
        ds.exit1_level = 110.0
        self.engine.order_cfg = {"target_exit": "EXIT1"}
        self.engine.mode = Mode.BACKTEST
        self.engine.results = []
        self.engine.log_fn = Mock()
        self.engine._VwapPiercingEngine__update_mae_mfe_range = Mock()
        self.engine._VwapPiercingEngine__stamp_mae_mfe = Mock()
        reset = Mock()
        self.engine._VwapPiercingEngine__reset_direction_backtest = reset
        bar = {
            DATE_TIME: "2026-11-05 10:00:00",
            OPEN_PRICE: 105.0,
            HIGH_PRICE: 111.0,
            LOW_PRICE: 100.0,
            CLOSE_PRICE: 108.0,
        }

        self.engine._VwapPiercingEngine__run_in_trade_minutes_backtest(ds, [bar])

        self.assertEqual(self.engine.results, [ds.current_trade])
        self.assertTrue(ds.current_trade.exit1_hit.is_hit)
        reset.assert_called_once_with(ds)

    def test_entry_fill_places_configured_target_order(self):
        broker = Mock()
        broker.OPTION_MARKET_TYPE = "OPT"
        self.engine.mode = Mode.LIVE
        self.engine.test_mode = False
        self.engine.place_orders = True
        self.engine.logic_name = "test"
        self.engine.option_type = "CE"
        self.engine.session_date_str = "2026-11-05"
        self.engine.trade_date_str = "2026-11-05"
        self.engine.candle_interval_minutes = 5
        self.engine.last_candle_data = None
        self.engine.order_cfg = {"target_exit": "EXIT1", "tick_size": 0.05}
        self.engine.trade_utility = SimpleNamespace(get_broker_utility=lambda: broker)
        self.engine.quotes_utility = SimpleNamespace(add_stocks=Mock())
        place_order = Mock(return_value=(True, "target-123"))
        self.engine._VwapPiercingEngine__place_real_order = place_order
        ds = _DirectionState("BUY")
        ds.piercing_candle = {
            DATE_TIME: "2026-11-05 09:30:00", OPEN_PRICE: 99.0, HIGH_PRICE: 105.0,
            LOW_PRICE: 98.0, CLOSE_PRICE: 104.0, VWAP: 100.0,
        }
        ds.reclaim_candle = {
            DATE_TIME: "2026-11-05 09:35:00", OPEN_PRICE: 103.0, HIGH_PRICE: 104.0,
            LOW_PRICE: 101.0, CLOSE_PRICE: 103.0, VWAP: 100.0,
        }
        ds.order_quantity = 30

        self.engine._VwapPiercingEngine__enter_trade(
            ds, 100.0, "09:45:00", entry_order_already_placed=True,
            filled_quantity=30, entry_order_symbol="NIFTY-CE")

        self.assertEqual(ds.exit_order_id, "target-123")
        place_order.assert_called_once_with(
            "NIFTY-CE", "SELL", 30, 107.0, "Target EXIT1", order_type="LIMIT",
            price_override=107.0, return_order_id=True)

    def test_fyers_market_modify_uses_market_order_type(self):
        adapter = object.__new__(fyers_utitlity)
        adapter.fyers = Mock()
        adapter.fyers.modify_order.return_value = {"s": "ok"}

        modified_id = adapter.modify_order(
            "exit-123", order_type="MARKET", price=0, trigger_price=0)

        self.assertEqual(modified_id, "exit-123")
        adapter.fyers.modify_order.assert_called_once_with({
            "id": "exit-123", "type": 2, "limitPrice": 0, "stopPrice": 0,
        })

    def test_test_mode_refreshes_historical_option_selection_each_main_candle(self):
        class Rows:
            def __init__(self, row):
                self.row = row

            def __getitem__(self, index):
                return self.row

        class CandleFrame:
            def __init__(self, row):
                self.iloc = Rows(row)

            def __len__(self):
                return 1

        row = {DATE_TIME: "2026-11-05 09:15:00"}
        self.engine.test_mode = True
        self.engine.trade_date_str = "2026-11-05"
        self.engine.candle_interval_minutes = 5
        self.engine.last_candle_data = CandleFrame(row)
        self.engine.last_test_option_refresh_ts = None
        self.engine.directions = {"BUY": _DirectionState("BUY")}
        refresh = Mock()
        self.engine._VwapPiercingEngine__refresh_signal_option_backtest = refresh

        self.engine._VwapPiercingEngine__refresh_signal_option_live()
        self.engine._VwapPiercingEngine__refresh_signal_option_live()
        refresh.assert_called_once_with("2026-11-05 09:19:00")

        self.engine.last_candle_data = CandleFrame({DATE_TIME: "2026-11-05 09:35:00"})
        self.engine._VwapPiercingEngine__refresh_signal_option_live()
        refresh.assert_called_with("2026-11-05 09:39:00")

        self.engine.directions["BUY"].state = "SEEK_RECLAIM"
        self.engine.last_candle_data = CandleFrame({DATE_TIME: "2026-11-05 09:40:00"})
        self.engine._VwapPiercingEngine__refresh_signal_option_live()
        self.assertEqual(refresh.call_count, 2)

    def test_test_mode_pinned_option_skips_historical_reselection(self):
        self.engine.test_mode = True
        self.engine.test_option_symbol = "NIFTY26N0924550CE"
        self.engine.last_candle_data = None
        self.engine.directions = {"BUY": _DirectionState("BUY")}
        refresh = Mock()
        self.engine._VwapPiercingEngine__refresh_signal_option_backtest = refresh

        self.engine._VwapPiercingEngine__refresh_signal_option_live()

        refresh.assert_not_called()

    def test_test_mode_prerequisite_loads_pinned_option(self):
        from threading import Event

        self.engine.test_mode = True
        self.engine.test_option_symbol = "NIFTY26N0924550CE"
        self.engine.option_type = "CE"
        self.engine.session_date_str = "2026-11-05"
        self.engine.logic_name = "test"
        self.engine.trade_utility = SimpleNamespace(get_broker_utility=lambda: Mock())
        self.engine.candle_interval_minutes = 5
        self.engine.test_option_selection_ready = False
        self.engine.preselected_options = {"CE": (None, 0.0), "PE": (None, 0.0)}
        self.engine.signal_option_symbol = ""
        self.engine.pre_requisite_complete_event = Event()
        load_series = Mock()
        self.engine._VwapPiercingEngine__test_day_series = load_series
        select_option = Mock(side_effect=AssertionError("pinned contract must bypass selection"))
        self.engine._VwapPiercingEngine__select_historical_option = select_option

        self.engine.pre_requisite_thread_handler()

        self.assertEqual(self.engine.signal_option_symbol, "NIFTY26N0924550CE")
        self.assertEqual(self.engine.preselected_options["CE"], ("NIFTY26N0924550CE", 0.0))
        self.assertTrue(self.engine.test_option_selection_ready)
        select_option.assert_not_called()
        self.assertEqual(load_series.call_count, 2)

    def test_kite_contract_strike_uses_instrument_metadata_not_expiry_digits(self):
        expiry = datetime.now().date() + timedelta(days=7)
        adapter = object.__new__(kite_utitlity)
        adapter.nfo_instrument_df = pd.DataFrame([{
            "tradingsymbol": "NIFTY26N0924550CE",
            "name": "NIFTY",
            "strike": 24550.0,
            "instrument_type": "CE",
            "expiry": expiry,
        }])
        contracts = adapter.get_option_contracts("NIFTY", expiry.strftime("%d-%b-%Y"), "CE")

        self.assertEqual(contracts, [{
            "symbol": "NIFTY26N0924550CE",
            "strike_price": 24550,
        }])

    def test_kite_option_chain_strike_uses_instrument_metadata(self):
        expiry = datetime.now().date() + timedelta(days=7)
        adapter = object.__new__(kite_utitlity)
        adapter.kite = Mock()
        adapter.kite.instruments.return_value = [{
            "tradingsymbol": "NIFTY26N0924550CE",
            "name": "NIFTY",
            "strike": 24550.0,
            "instrument_type": "CE",
            "expiry": expiry,
            "instrument_token": 123,
        }]
        adapter.kite.ltp.return_value = {
            "NFO:NIFTY26N0924550CE": {"last_price": 100.0},
        }

        chain, _, _ = adapter.getOptionChain("NIFTY")

        self.assertEqual(chain.iloc[0]["strike_price"], 24550)

    def test_test_mode_bootstraps_nearest_strike_when_startup_premium_out_of_band(self):
        broker = Mock()
        broker.get_option_contracts.return_value = [
            {"symbol": "NIFTY26N0924550CE", "strike_price": 24550},
            {"symbol": "NIFTY26N0924600CE", "strike_price": 24600},
        ]
        broker.get_historical_option_center.return_value = 24550
        self.engine.broker = broker
        self.engine.index_name = "NIFTY"
        self.engine.trade_date_str = "2026-11-05"
        self.engine.log_fn = Mock()

        symbol = self.engine._VwapPiercingEngine__historical_option_bootstrap(
            "CE", "2026-11-05 09:16:15")

        self.assertEqual(symbol, "NIFTY26N0924550CE")

    def test_order_status_normalization(self):
        normalize = VwapPiercingEngine._VwapPiercingEngine__normalized_order_status

        class Order:
            def __init__(self, status):
                self.status = status

        self.assertEqual(normalize(Order("Complete")), "complete")
        self.assertEqual(normalize(Order("Trigger Pending")), "open")
        self.assertEqual(normalize(Order("CANCELLED")), "failed")
        self.assertEqual(normalize(Order("unexpected")), "unknown")


if __name__ == "__main__":
    unittest.main()
