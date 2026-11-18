import unittest
from unittest.mock import Mock

from BusinessLogic.vwap_piercing_option_buy.Logic.piercing_engine import (
    VwapPiercingEngine as BuyPiercingEngine,
)
from BusinessLogic.vwappiercing_options.Logic.piercing_engine import (
    VwapPiercingEngine as SellPiercingEngine,
)
from executor import wait_for_logic_interfaces


class _LogicInterface:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def wait_for_completion(self):
        self.events.append(f"wait:{self.name}")

    def force_close_open_trade(self):
        self.events.append(f"close:{self.name}")


class _Thread:
    def __init__(self, events):
        self.events = events

    def join(self):
        self.events.append("join:quotes")


class _QuotesUtility:
    def __init__(self, events):
        self.events = events

    def stop(self):
        self.events.append("stop:quotes")

    def get_thread_info(self):
        self.events.append("get:quotes-thread")
        return _Thread(self.events)


class ExecutorLifecycleTests(unittest.TestCase):
    def test_strategy_exit_threads_do_not_stop_shared_quotes(self):
        for engine_type in (BuyPiercingEngine, SellPiercingEngine):
            with self.subTest(engine=engine_type):
                engine = object.__new__(engine_type)
                engine.logic_name = engine_type.__module__
                engine.execute_thread = Mock()
                engine.quotes_utility = Mock()

                engine.exit_execution_thread()

                engine.execute_thread.join.assert_called_once_with()
                engine.quotes_utility.stop.assert_not_called()
                engine.quotes_utility.get_thread_info.assert_not_called()

    def test_shared_quotes_stop_after_all_logics_complete(self):
        events = []
        interfaces = [
            _LogicInterface("first", events),
            _LogicInterface("second", events),
        ]

        wait_for_logic_interfaces(interfaces, _QuotesUtility(events))

        self.assertEqual(events, [
            "wait:first",
            "close:first",
            "wait:second",
            "close:second",
            "stop:quotes",
            "get:quotes-thread",
            "join:quotes",
        ])

    def test_shared_quotes_stop_even_if_logic_completion_fails(self):
        events = []

        class _FailingLogicInterface(_LogicInterface):
            def wait_for_completion(self):
                raise RuntimeError("logic failed")

        with self.assertRaisesRegex(RuntimeError, "logic failed"):
            wait_for_logic_interfaces(
                [_FailingLogicInterface("first", events)],
                _QuotesUtility(events),
            )

        self.assertEqual(events, [
            "stop:quotes",
            "get:quotes-thread",
            "join:quotes",
        ])


if __name__ == "__main__":
    unittest.main()
