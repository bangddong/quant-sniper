import json
import unittest
from pathlib import Path

from coin_sniper.config import Settings
from coin_sniper.exchange.binance_feed import BinanceFeed, subscription_requests

KLINE = {"e": "kline", "E": 1731801700000, "s": "BTCUSDT",
         "k": {"t": 1731801600000, "o": "90000", "h": "91000", "l": "89000", "c": "90500",
               "v": "12", "q": "1080000"}}


def make_settings():
    return Settings(mode="demo", rest_base="https://demo-api.binance.com", stream_base="wss://x",
                    api_key="", api_secret="", state_dir=Path("."), starting_capital=1000.0,
                    auto_git_push=False)


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def kline(symbol="BTCUSDT", close="90500"):
    event = json.loads(json.dumps(KLINE))
    event["s"] = symbol
    event["k"]["c"] = close
    return json.dumps(event)


class BinanceFeedTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.feed = BinanceFeed(make_settings(), clock=self.clock)

    def test_kline_message_becomes_quote(self):
        self.feed.handle_message(json.dumps(KLINE))
        q = self.feed.quote("BTCUSDT")
        self.assertEqual(q.symbol, "BTCUSDT")
        self.assertEqual(q.price, 90500)
        self.assertEqual(q.open, 90000)
        self.assertEqual(q.high, 91000)
        self.assertEqual(q.low, 89000)
        self.assertEqual(q.volume, 12)
        self.assertEqual(q.quote_volume, 1080000)
        self.assertEqual(q.day, "2024-11-17")
        self.assertEqual(q.received_at, self.clock.now)

    def test_stale_quote_returns_none(self):
        self.feed.handle_message(json.dumps(KLINE))
        self.clock.now += 15
        self.assertIsNotNone(self.feed.quote("BTCUSDT"))
        self.clock.now += 1
        self.assertIsNone(self.feed.quote("BTCUSDT"))
        self.assertIsNotNone(self.feed.quote("BTCUSDT", max_age=60))

    def test_unknown_symbol_returns_none(self):
        self.assertIsNone(self.feed.quote("ETHUSDT"))

    def test_subscription_payload(self):
        self.feed.set_symbols(["BTCUSDT", "ETHUSDT"])
        reqs = subscription_requests(set(), {"BTCUSDT", "ETHUSDT"}, 1)
        self.assertEqual(reqs, [{"method": "SUBSCRIBE",
                                 "params": ["btcusdt@kline_1d", "ethusdt@kline_1d"], "id": 1}])

    def test_subscription_diff_and_ids(self):
        reqs = subscription_requests({"BTCUSDT", "ETHUSDT"}, {"ETHUSDT", "SOLUSDT", "ADAUSDT"}, 7)
        self.assertEqual(reqs, [
            {"method": "UNSUBSCRIBE", "params": ["btcusdt@kline_1d"], "id": 7},
            {"method": "SUBSCRIBE", "params": ["adausdt@kline_1d", "solusdt@kline_1d"], "id": 8}])
        self.assertEqual(subscription_requests({"BTCUSDT"}, {"BTCUSDT"}, 1), [])

    def test_malformed_message_is_ignored(self):
        for raw in ["not json", "", "[]", "null", "42", '{"e":"kline"}', '{"e":"kline","k":5}',
                    '{"e":"kline","s":"BTCUSDT","k":{"t":"x","c":"abc"}}',
                    '{"e":"kline","s":"BTCUSDT","k":{"t":1731801600000}}',
                    '{"result":null,"id":1}', '{"e":"trade","s":"BTCUSDT"}', None, b"\xff"]:
            self.feed.handle_message(raw)
        self.assertIsNone(self.feed.quote("BTCUSDT"))

    def test_non_finite_or_non_positive_prices_are_ignored(self):
        for field, value in (("c", "0"), ("o", "-1"), ("h", "nan"), ("l", "inf"), ("c", "NaN"), ("o", "0.0")):
            event = json.loads(json.dumps(KLINE))
            event["k"][field] = value
            self.feed.handle_message(json.dumps(event))
            self.assertIsNone(self.feed.quote("BTCUSDT"), (field, value))

    def test_quote_returns_copy(self):
        self.feed.handle_message(json.dumps(KLINE))
        self.feed.quote("BTCUSDT").price = 1.0
        self.assertEqual(self.feed.quote("BTCUSDT").price, 90500)

    def test_latest_kline_replaces_previous(self):
        self.feed.handle_message(kline(close="90500"))
        self.feed.handle_message(kline(close="91000"))
        self.assertEqual(self.feed.quote("BTCUSDT").price, 91000)

    def test_set_symbols_normalizes_and_removal_drops_quotes(self):
        self.feed.set_symbols(["btcusdt", "BTCUSDT", "ethusdt"])
        self.assertEqual(self.feed._symbols_snapshot(), {"BTCUSDT", "ETHUSDT"})
        self.feed.handle_message(kline("BTCUSDT"))
        self.feed.handle_message(kline("ETHUSDT"))
        self.feed.set_symbols(["ETHUSDT"])
        self.assertIsNone(self.feed.quote("BTCUSDT"))
        self.assertIsNotNone(self.feed.quote("ETHUSDT"))

    def test_reconnect_counter_drains(self):
        calls = []
        feed = BinanceFeed(make_settings(), on_reconnect=lambda: calls.append(1), clock=self.clock)
        self.assertEqual(feed.take_reconnect_count(), 0)
        feed._note_attempt()          # 첫 접속은 재접속이 아니다
        self.assertEqual(feed.take_reconnect_count(), 0)
        feed._note_attempt()
        feed._note_attempt()
        self.assertEqual(calls, [1, 1])
        self.assertEqual(feed.take_reconnect_count(), 2)
        self.assertEqual(feed.take_reconnect_count(), 0)

    def test_health_starts_disconnected(self):
        self.assertEqual(self.feed.health(), (False, ""))
        self.feed._set_health(True)
        self.assertEqual(self.feed.health(), (True, ""))
        self.feed._set_health(False, ValueError("boom"))
        self.assertEqual(self.feed.health(), (False, "ValueError: boom"))


if __name__ == "__main__":
    unittest.main()
