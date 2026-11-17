"""데모 모드 확인 스크립트 테스트. 네트워크·실제 sleep 없이 가짜 거래소/피드로 검증한다."""
from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from unittest import mock

from coin_sniper import probe
from coin_sniper.config import Settings
from coin_sniper.exchange.fake import FakeExchange, FakeFeed
from coin_sniper.models import ApiResult, SymbolRules
from helpers import trending_candles

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
SENTINEL = "SENTINEL-SECRET-XYZ"


def _rule(symbol, step=0.00001):
    return SymbolRules(symbol=symbol, base=symbol[:-4], quote="USDT", trading=True,
                       step_size=step, tick_size=0.01, min_notional=5.0)


def make_world():
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "USDCUSDT"]
    ex = FakeExchange({s: _rule(s) for s in symbols}, {"USDT": 10000.0, "ETH": 1.5})
    for s in symbols:
        ex.set_price(s, 90000.0 if s == "BTCUSDT" else 100.0)
        ex.set_candles(s, trending_candles(70, 100.0, 1.0, quote_vol=5e7))
    ex.set_candles("BTCUSDT", trending_candles(70, 89000.0, 10.0, quote_vol=5e7))
    feed = FakeFeed()
    feed.set_quote("BTCUSDT", 90000.0, volume=10.0, day="2026-11-17")
    return ex, feed


def run(ex, feed):
    sleeps = []
    results = probe.run_probe(ex, feed, sleeps.append, lambda: NOW)
    return results, sleeps


def by_id(results):
    return {r["id"]: r for r in results}


def sells(ex):
    return [c for c in ex.calls if c[0] == "market_sell"]


class RunProbeTests(unittest.TestCase):
    def test_all_pass_and_position_flat(self):
        ex, feed = make_world()
        results, _ = run(ex, feed)
        self.assertEqual([r["id"] for r in results], [1, 2, 3, 4, 5, 6, 7])
        for r in results:
            self.assertEqual(r["status"], "PASS", r)
        free, locked = ex.balances().value["BTC"]
        self.assertLess(free, 0.00001)          # 수수료와 단계 내림 먼지만 남는다
        self.assertEqual(locked, 0.0)
        self.assertEqual(ex.open_orders().value, [])
        self.assertEqual(ex.balances().value["ETH"], (1.5, 0.0))
        self.assertEqual(len(sells(ex)), 1)
        self.assertEqual(feed.symbols, ["BTCUSDT"])
        self.assertEqual(feed.started, 1)

    def test_only_btc_traded(self):
        ex, feed = make_world()
        run(ex, feed)
        for name, args in ex.calls:
            if name in ("market_buy", "market_sell", "place_stop", "cancel_order"):
                self.assertEqual(args["symbol"], "BTCUSDT")

    def test_stop_failure_still_sells(self):
        ex, feed = make_world()
        ex.fail_next("place_stop", ApiResult(ok=False, kind="permanent", error="HTTP 400 code -1013: bad"))
        results, _ = run(ex, feed)
        r = by_id(results)
        self.assertEqual(r[5]["status"], "FAIL")
        self.assertIn("-1013", r[5]["observed"])
        self.assertEqual(r[6]["status"], "PASS")
        self.assertEqual(len(sells(ex)), 1)
        self.assertFalse(all(x["status"] == "PASS" for x in results))

    def test_buy_failure_skips_stop_and_sell(self):
        ex, feed = make_world()
        ex.fail_next("market_buy", ApiResult(ok=False, kind="permanent", error="nope"))
        results, _ = run(ex, feed)
        r = by_id(results)
        self.assertEqual(r[4]["status"], "FAIL")
        self.assertEqual(r[5]["status"], "SKIP")
        self.assertEqual(r[6]["status"], "SKIP")
        self.assertEqual(r[7]["status"], "PASS")
        self.assertEqual(sells(ex), [])
        self.assertFalse([c for c in ex.calls if c[0] == "place_stop"])

    def test_check1_failure_does_not_stop_others(self):
        ex, feed = make_world()
        ex.fail_next("server_time", ApiResult(ok=False, kind="transient", error="boom"))
        results, _ = run(ex, feed)
        r = by_id(results)
        self.assertEqual(r[1]["status"], "FAIL")
        for i in range(2, 8):
            self.assertEqual(r[i]["status"], "PASS", r[i])

    def test_exception_becomes_fail(self):
        ex, feed = make_world()

        def explode():
            raise RuntimeError("kaboom")
        ex.server_time = explode
        results, _ = run(ex, feed)
        r = by_id(results)
        self.assertEqual(r[1]["status"], "FAIL")
        self.assertIn("RuntimeError", r[1]["observed"])
        self.assertIn("kaboom", r[1]["observed"])
        self.assertEqual(r[2]["status"], "PASS")

    def test_stream_failure_without_quote(self):
        ex, feed = make_world()
        feed._quotes.clear()
        feed.set_health(False, "connection refused")
        results, sleeps = run(ex, feed)
        r = by_id(results)
        self.assertEqual(r[7]["status"], "FAIL")
        self.assertIn("connection refused", r[7]["observed"])
        self.assertEqual(sleeps.count(1), 9)

    def test_stream_wrong_day_fails(self):
        ex, feed = make_world()
        feed.set_quote("BTCUSDT", 90000.0, day="2026-11-16")
        results, _ = run(ex, feed)
        self.assertEqual(by_id(results)[7]["status"], "FAIL")

    def test_universe_count_below_minimum_fails(self):
        ex, feed = make_world()
        for s in ("ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT"):
            ex.set_candles(s, trending_candles(10, 100.0, 1.0))
        results, _ = run(ex, feed)
        self.assertEqual(by_id(results)[3]["status"], "FAIL")

    def test_candle_pauses_use_injected_sleep(self):
        ex, feed = make_world()
        _, sleeps = run(ex, feed)
        self.assertGreaterEqual(sleeps.count(0.1), 5)

    def test_leftover_stop_cancelled_before_sell(self):
        ex, feed = make_world()
        ex.fail_next("cancel_order", ApiResult(ok=False, kind="transient", error="net"))
        results, _ = run(ex, feed)
        r = by_id(results)
        self.assertEqual(r[5]["status"], "FAIL")
        self.assertEqual(r[6]["status"], "PASS")
        self.assertEqual(ex.open_orders().value, [])


class FixRound1Tests(unittest.TestCase):
    def test_lost_buy_response_still_sold(self):
        ex, feed = make_world()
        ex.drop_response_next("market_buy")
        results, _ = run(ex, feed)
        r = by_id(results)
        self.assertEqual(r[4]["status"], "FAIL")
        self.assertEqual(r[5]["status"], "PASS")
        self.assertEqual(r[6]["status"], "PASS")
        self.assertLess(ex.balances().value["BTC"][0], 0.00001)
        self.assertEqual(len(sells(ex)), 1)

    def test_unknown_buy_not_sold_blindly_and_reported(self):
        ex, feed = make_world()
        ex.drop_response_next("market_buy")
        ex.hide_order_for(30)
        results, sleeps = run(ex, feed)
        r = by_id(results)
        self.assertEqual(r[4]["status"], "FAIL")
        buy_id = [c for c in ex.calls if c[0] == "market_buy"][0][1]["client_id"]
        self.assertIn(buy_id, r[4]["observed"])
        self.assertIn("남았을 수", r[4]["observed"])
        self.assertEqual(sells(ex), [])
        self.assertEqual(r[6]["status"], "FAIL")
        text = probe.render_report(results, NOW)
        self.assertIn(buy_id, text)
        self.assertEqual(sleeps.count(1), 9)      # 4번 재조회 9회(7번은 첫 조회에서 통과)

    def test_lost_stop_response_cancelled_before_sell(self):
        ex, feed = make_world()
        ex.drop_response_next("place_stop")
        results, _ = run(ex, feed)
        r = by_id(results)
        self.assertEqual(r[5]["status"], "FAIL")
        self.assertIn("열려 있을 수", r[5]["observed"])
        self.assertEqual(r[6]["status"], "PASS")
        names = [c[0] for c in ex.calls]
        self.assertLess(names.index("cancel_order"), names.index("market_sell"))
        self.assertEqual(ex.open_orders().value, [])

    def test_cancel_failing_twice_reports_open_stop(self):
        ex, feed = make_world()
        fail = ApiResult(ok=False, kind="transient", error="net")
        ex.fail_next("cancel_order", fail, times=2)
        results, _ = run(ex, feed)
        r = by_id(results)
        self.assertEqual(r[6]["status"], "FAIL")
        self.assertIn("취소 실패", r[6]["observed"])
        stop_id = [c for c in ex.calls if c[0] == "place_stop"][0][1]["client_id"]
        text = probe.render_report(results, NOW)
        self.assertIn(stop_id, text)
        self.assertIn("직접 취소", text)

    def test_invalid_then_valid_quote_passes(self):
        ex, feed = make_world()
        feed.set_quote("BTCUSDT", 90000.0, day="2026-11-16")

        def sleep(sec):
            if sec == 1:
                feed.set_quote("BTCUSDT", 90000.0, volume=1.0, day="2026-11-17")
        results = probe.run_probe(ex, feed, sleep, lambda: NOW)
        self.assertEqual(by_id(results)[7]["status"], "PASS")

    def test_never_valid_polls_ten_sleeps_nine(self):
        ex, feed = make_world()
        feed.set_quote("BTCUSDT", 90000.0, day="2026-11-16")
        polls = []
        orig = feed.quote
        feed.quote = lambda s, *a: (polls.append(1), orig(s, *a))[1]
        results, sleeps = run(ex, feed)
        self.assertEqual(by_id(results)[7]["status"], "FAIL")
        self.assertEqual(len(polls), 10)
        self.assertEqual(sleeps.count(1), 9)


class RenderTests(unittest.TestCase):
    def test_report_contents_and_no_secrets(self):
        ex, feed = make_world()
        results, _ = run(ex, feed)
        results[4]["status"] = "FAIL"
        results[4]["observed"] = f"error with {SENTINEL} inside"
        settings = Settings(mode="demo", rest_base="https://demo-api.binance.com",
                            stream_base="wss://demo-stream.binance.com/ws", api_key=SENTINEL,
                            api_secret="SENTINEL-OTHER", state_dir=None, starting_capital=1.0,
                            auto_git_push=False)
        text = probe.render_report(results, NOW, settings)
        for r in results:
            self.assertIn(r["title"], text)
        for status in ("PASS", "FAIL"):
            self.assertIn(status, text)
        self.assertIn("demo-api.binance.com", text)
        self.assertIn("PROTECTIVE_STOPS_ENABLED = False", text)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("SENTINEL-OTHER", text)
        self.assertIn("21:00", text)             # KST = UTC+9

    def test_report_without_settings(self):
        text = probe.render_report([{"id": 1, "title": "t|x", "status": "SKIP", "observed": "a\nb"}], NOW)
        self.assertIn("SKIP", text)


class MainTests(unittest.TestCase):
    def test_refuses_live(self):
        env = {"MODE": "live", "LIVE_TRADING_ENABLED": "true",
               "BINANCE_LIVE_API_KEY": "k", "BINANCE_LIVE_API_SECRET": "s"}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("env_loader.load_dotenv"), \
                mock.patch.object(probe, "BinanceExchange") as be, redirect_stderr(err):
            code = probe.main([])
        self.assertEqual(code, 2)
        be.assert_not_called()
        self.assertIn("demo", err.getvalue())

    def test_missing_keys_exit_2(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"MODE": "demo"}, clear=True), \
                mock.patch("env_loader.load_dotenv"), redirect_stderr(err):
            code = probe.main([])
        self.assertEqual(code, 2)
        self.assertTrue(err.getvalue())


if __name__ == "__main__":
    unittest.main()
