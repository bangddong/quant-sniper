import unittest
from unittest import mock

from coin_sniper import config

from coin_sniper.exchange.fake import FakeExchange, FakeFeed
from coin_sniper.models import ApiResult, SymbolRules


def make(balances=None, fee_rate=0.001, price=100.0):
    rules = {"ABCUSDT": SymbolRules("ABCUSDT", "ABC", "USDT", True, 0.1, 0.01, 10.0)}
    ex = FakeExchange(rules, balances if balances is not None else {"USDT": 1000.0}, fee_rate)
    if price is not None:
        ex.set_price("ABCUSDT", price)
    return ex


def bal(ex):
    return ex.balances().value


class MarketBuyTests(unittest.TestCase):
    def test_buy_fills_at_set_price_and_charges_fee_in_base(self):
        ex = make()
        r = ex.market_buy("ABCUSDT", 2.0, "b1")
        self.assertTrue(r.ok)
        o = r.value
        self.assertEqual((o.status, o.side, o.exists), ("FILLED", "BUY", True))
        self.assertAlmostEqual(o.executed_qty, 2.0)
        self.assertAlmostEqual(o.quote_qty, 200.0)
        self.assertEqual(list(o.fees), ["ABC"])
        self.assertAlmostEqual(o.fees["ABC"], 0.002)
        b = bal(ex)
        self.assertAlmostEqual(b["ABC"][0], 1.998)
        self.assertAlmostEqual(b["USDT"][0], 800.0)

    def test_buy_without_enough_quote_is_permanent_2010(self):
        ex = make({"USDT": 100.0})
        r = ex.market_buy("ABCUSDT", 2.0, "b1")
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "permanent")
        self.assertIn("-2010", r.error)
        self.assertEqual(bal(ex)["USDT"], (100.0, 0.0))

    def test_no_price_set_is_permanent(self):
        ex = make(price=None)
        r = ex.market_buy("ABCUSDT", 2.0, "b1")
        self.assertEqual((r.ok, r.kind), (False, "permanent"))

    def test_unknown_symbol_is_permanent(self):
        r = make().market_buy("ZZZUSDT", 2.0, "b1")
        self.assertEqual((r.ok, r.kind), (False, "permanent"))


class MarketSellTests(unittest.TestCase):
    def test_sell_fee_in_quote(self):
        ex = make({"USDT": 0.0, "ABC": 3.0})
        r = ex.market_sell("ABCUSDT", 2.0, "s1")
        self.assertTrue(r.ok)
        o = r.value
        self.assertEqual((o.status, o.side), ("FILLED", "SELL"))
        self.assertAlmostEqual(o.quote_qty, 200.0)
        self.assertEqual(list(o.fees), ["USDT"])
        self.assertAlmostEqual(o.fees["USDT"], 0.2)
        b = bal(ex)
        self.assertAlmostEqual(b["USDT"][0], 199.8)
        self.assertAlmostEqual(b["ABC"][0], 1.0)

    def test_sell_more_than_free_is_permanent_2010(self):
        ex = make({"ABC": 1.0})
        r = ex.market_sell("ABCUSDT", 2.0, "s1")
        self.assertEqual((r.ok, r.kind), (False, "permanent"))
        self.assertIn("-2010", r.error)
        self.assertEqual(bal(ex)["ABC"], (1.0, 0.0))


class ValidationTests(unittest.TestCase):
    def test_step_violation_is_1013(self):
        ex = make()
        r = ex.market_buy("ABCUSDT", 1.05, "b1")
        self.assertEqual((r.ok, r.kind), (False, "permanent"))
        self.assertIn("-1013", r.error)

    def test_decimal_multiple_check_accepts_0_3(self):
        ex = make()
        self.assertTrue(ex.market_buy("ABCUSDT", 0.3, "b1").ok)

    def test_below_min_notional_is_1013(self):
        ex = make()
        r = ex.market_buy("ABCUSDT", 0.1, "b1")      # 10.0 notional == min -> ok
        self.assertTrue(r.ok)
        ex.set_price("ABCUSDT", 99.0)
        r = ex.market_buy("ABCUSDT", 0.1, "b2")      # 9.9 < 10
        self.assertEqual((r.ok, r.kind), (False, "permanent"))
        self.assertIn("-1013", r.error)

    def test_stop_price_tick_violation_is_1013(self):
        ex = make({"ABC": 5.0})
        r = ex.place_stop("ABCUSDT", 1.0, 90.005, 89.0, "p1")
        self.assertEqual((r.ok, r.kind), (False, "permanent"))
        self.assertIn("-1013", r.error)
        r = ex.place_stop("ABCUSDT", 1.0, 90.0, 89.001, "p2")
        self.assertIn("-1013", r.error)
        self.assertEqual(bal(ex)["ABC"], (5.0, 0.0))

    def test_stop_step_and_notional_validated(self):
        ex = make({"ABC": 5.0})
        self.assertIn("-1013", ex.place_stop("ABCUSDT", 1.05, 90.0, 89.0, "p1").error)
        self.assertIn("-1013", ex.place_stop("ABCUSDT", 0.1, 90.0, 89.0, "p2").error)

    def test_trading_false_blocks_orders_and_reflected_in_rules(self):
        ex = make({"USDT": 1000.0, "ABC": 5.0})
        ex.set_trading("ABCUSDT", False)
        self.assertFalse(ex.symbol_rules().value["ABCUSDT"].trading)
        for r in (ex.market_buy("ABCUSDT", 1.0, "b1"), ex.market_sell("ABCUSDT", 1.0, "s1"),
                  ex.place_stop("ABCUSDT", 1.0, 90.0, 89.0, "p1")):
            self.assertEqual((r.ok, r.kind), (False, "permanent"))
        ex.set_trading("ABCUSDT", True)
        self.assertTrue(ex.symbol_rules().value["ABCUSDT"].trading)
        self.assertTrue(ex.market_buy("ABCUSDT", 1.0, "b2").ok)


class DuplicateTests(unittest.TestCase):
    def test_duplicate_client_id_permanent_any_type(self):
        ex = make({"USDT": 1000.0, "ABC": 5.0})
        self.assertTrue(ex.market_buy("ABCUSDT", 1.0, "x").ok)
        for r in (ex.market_buy("ABCUSDT", 1.0, "x"), ex.market_sell("ABCUSDT", 1.0, "x"),
                  ex.place_stop("ABCUSDT", 1.0, 90.0, 89.0, "x")):
            self.assertEqual((r.ok, r.kind), (False, "permanent"))
        self.assertAlmostEqual(bal(ex)["USDT"][0], 900.0)

    def test_stop_id_cannot_be_reused_by_market(self):
        ex = make({"ABC": 5.0})
        self.assertTrue(ex.place_stop("ABCUSDT", 1.0, 90.0, 89.0, "x").ok)
        self.assertFalse(ex.market_sell("ABCUSDT", 1.0, "x").ok)


class StopTests(unittest.TestCase):
    def test_place_stop_locks_quantity(self):
        ex = make({"ABC": 5.0})
        r = ex.place_stop("ABCUSDT", 2.0, 90.0, 89.0, "p1")
        self.assertTrue(r.ok)
        self.assertEqual((r.value.status, r.value.side), ("NEW", "SELL"))
        self.assertEqual(bal(ex)["ABC"], (3.0, 2.0))

    def test_selling_locked_quantity_is_permanent_2010(self):
        ex = make({"ABC": 5.0})
        ex.place_stop("ABCUSDT", 4.0, 90.0, 89.0, "p1")
        r = ex.market_sell("ABCUSDT", 2.0, "s1")
        self.assertEqual((r.ok, r.kind), (False, "permanent"))
        self.assertIn("-2010", r.error)

    def test_place_stop_beyond_free_is_2010(self):
        ex = make({"ABC": 1.0})
        r = ex.place_stop("ABCUSDT", 2.0, 90.0, 89.0, "p1")
        self.assertEqual((r.ok, r.kind), (False, "permanent"))
        self.assertIn("-2010", r.error)

    def test_cancel_unlocks(self):
        ex = make({"ABC": 5.0})
        ex.place_stop("ABCUSDT", 2.0, 90.0, 89.0, "p1")
        r = ex.cancel_order("ABCUSDT", "p1")
        self.assertTrue(r.ok)
        self.assertEqual(r.value.status, "CANCELED")
        self.assertEqual(bal(ex)["ABC"], (5.0, 0.0))
        self.assertEqual(ex.get_order("ABCUSDT", "p1").value.status, "CANCELED")
        self.assertEqual(ex.open_orders().value, [])

    def test_cancel_unknown_is_2011(self):
        r = make().cancel_order("ABCUSDT", "nope")
        self.assertEqual((r.ok, r.kind), (False, "permanent"))
        self.assertIn("-2011", r.error)

    def test_cancel_filled_is_2011(self):
        ex = make()
        ex.market_buy("ABCUSDT", 1.0, "b1")
        r = ex.cancel_order("ABCUSDT", "b1")
        self.assertEqual((r.ok, r.kind), (False, "permanent"))
        self.assertIn("-2011", r.error)

    def test_cancel_already_canceled_is_2011(self):
        ex = make({"ABC": 5.0})
        ex.place_stop("ABCUSDT", 2.0, 90.0, 89.0, "p1")
        ex.cancel_order("ABCUSDT", "p1")
        self.assertIn("-2011", ex.cancel_order("ABCUSDT", "p1").error)
        self.assertEqual(bal(ex)["ABC"], (5.0, 0.0))

    def test_market_stop_fills_at_current_price_even_below_limit(self):
        ex = make({"USDT": 0.0, "ABC": 5.0})
        ex.place_stop("ABCUSDT", 2.0, 90.0, 89.0, "p1")   # 기본 STOP_LOSS: 지정가는 쓰지 않는다
        ex.set_price("ABCUSDT", 80.0)                    # 갭으로 지정가 아래까지
        ex.trigger_stops()
        o = ex.get_order("ABCUSDT", "p1").value
        self.assertEqual(o.status, "FILLED")
        self.assertAlmostEqual(o.quote_qty, 160.0)

    @mock.patch.object(config, "PROTECTIVE_STOP_TYPE", "STOP_LOSS_LIMIT")   # 지정가 체결을 고정하는 테스트
    def test_trigger_fills_at_limit_price_when_price_at_or_below_stop(self):
        ex = make({"USDT": 0.0, "ABC": 5.0})
        ex.place_stop("ABCUSDT", 2.0, 90.0, 89.0, "p1")
        ex.trigger_stops()                       # price 100 > 90: nothing
        self.assertEqual(ex.get_order("ABCUSDT", "p1").value.status, "NEW")
        ex.set_price("ABCUSDT", 90.0)            # == stop price triggers
        ex.trigger_stops()
        o = ex.get_order("ABCUSDT", "p1").value
        self.assertEqual(o.status, "FILLED")
        self.assertAlmostEqual(o.executed_qty, 2.0)
        self.assertAlmostEqual(o.quote_qty, 178.0)
        self.assertEqual(list(o.fees), ["USDT"])
        self.assertAlmostEqual(o.fees["USDT"], 0.178)
        b = bal(ex)
        self.assertAlmostEqual(b["USDT"][0], 177.822)
        self.assertEqual(b["ABC"], (3.0, 0.0))
        self.assertEqual(ex.open_orders().value, [])

    def test_no_trigger_while_price_above_stop(self):
        ex = make({"ABC": 5.0})
        ex.place_stop("ABCUSDT", 2.0, 90.0, 89.0, "p1")
        ex.set_price("ABCUSDT", 95.0)
        ex.trigger_stops()
        self.assertEqual(ex.get_order("ABCUSDT", "p1").value.status, "NEW")


class QueryTests(unittest.TestCase):
    def test_get_order_unknown_matches_real_adapter(self):
        r = make().get_order("ABCUSDT", "nope")
        self.assertTrue(r.ok)
        self.assertFalse(r.value.exists)
        self.assertEqual(r.value.client_id, "nope")

    def test_returned_states_are_snapshots(self):
        ex = make({"ABC": 5.0})
        placed = ex.place_stop("ABCUSDT", 1.0, 90.0, 89.0, "p1").value
        ex.set_price("ABCUSDT", 80.0)
        ex.trigger_stops()
        self.assertEqual(placed.status, "NEW")
        self.assertEqual(ex.get_order("ABCUSDT", "p1").value.status, "FILLED")

    def test_get_order_known_has_fees(self):
        ex = make()
        ex.market_buy("ABCUSDT", 1.0, "b1")
        o = ex.get_order("ABCUSDT", "b1").value
        self.assertTrue(o.exists)
        self.assertAlmostEqual(o.fees["ABC"], 0.001)

    def test_open_orders_filters_by_symbol(self):
        rules = {"ABCUSDT": SymbolRules("ABCUSDT", "ABC", "USDT", True, 0.1, 0.01, 10.0),
                 "XYZUSDT": SymbolRules("XYZUSDT", "XYZ", "USDT", True, 0.1, 0.01, 10.0)}
        ex = FakeExchange(rules, {"ABC": 5.0, "XYZ": 5.0})
        ex.set_price("ABCUSDT", 100.0)
        ex.set_price("XYZUSDT", 100.0)
        ex.place_stop("ABCUSDT", 1.0, 90.0, 89.0, "a")
        ex.place_stop("XYZUSDT", 1.0, 90.0, 89.0, "x")
        self.assertEqual({o.client_id for o in ex.open_orders().value}, {"a", "x"})
        self.assertEqual([o.client_id for o in ex.open_orders("XYZUSDT").value], ["x"])

    def test_balances_include_every_asset_ever_held(self):
        ex = make()
        ex.market_buy("ABCUSDT", 1.0, "b1")
        ex.market_sell("ABCUSDT", 0.9, "s1")
        self.assertIn("ABC", bal(ex))
        ex2 = make({"USDT": 10.0})
        self.assertEqual(ex2.balances().value, {"USDT": (10.0, 0.0)})

    def test_candles(self):
        ex = make()
        r = ex.daily_candles("ABCUSDT", 5)
        self.assertEqual((r.ok, r.value), (True, []))
        ex.set_candles("ABCUSDT", [{"date": str(i)} for i in range(10)])
        got = ex.daily_candles("ABCUSDT", 3).value
        self.assertEqual([c["date"] for c in got], ["7", "8", "9"])

    def test_server_time_fixed(self):
        ex = make()
        a, b = ex.server_time(), ex.server_time()
        self.assertTrue(a.ok)
        self.assertEqual(a.value, b.value)


class FillRatioTests(unittest.TestCase):
    def test_partial_fill_expires_and_applies_once(self):
        ex = make()
        ex.set_fill_ratio(0.5)
        o = ex.market_buy("ABCUSDT", 1.0, "b1").value
        self.assertEqual(o.status, "EXPIRED")
        self.assertAlmostEqual(o.executed_qty, 0.5)
        self.assertAlmostEqual(o.quote_qty, 50.0)
        self.assertAlmostEqual(bal(ex)["ABC"][0], 0.4995)
        self.assertAlmostEqual(bal(ex)["USDT"][0], 950.0)
        o2 = ex.market_buy("ABCUSDT", 1.0, "b2").value
        self.assertEqual(o2.status, "FILLED")
        self.assertAlmostEqual(o2.executed_qty, 1.0)

    def test_floor_to_step_applied(self):
        ex = make()
        ex.set_fill_ratio(0.55)
        o = ex.market_buy("ABCUSDT", 1.0, "b1").value
        self.assertAlmostEqual(o.executed_qty, 0.5)

    def test_zero_ratio_expires_with_nothing(self):
        ex = make()
        ex.set_fill_ratio(0.0)
        o = ex.market_buy("ABCUSDT", 1.0, "b1").value
        self.assertEqual((o.status, o.executed_qty, o.quote_qty, o.fees), ("EXPIRED", 0.0, 0.0, {}))
        self.assertEqual(bal(ex)["USDT"], (1000.0, 0.0))

    def test_partial_sell_fee_in_quote(self):
        ex = make({"ABC": 2.0})
        ex.set_fill_ratio(0.5)
        o = ex.market_sell("ABCUSDT", 2.0, "s1").value
        self.assertEqual(o.status, "EXPIRED")
        self.assertAlmostEqual(bal(ex)["ABC"][0], 1.0)
        self.assertAlmostEqual(bal(ex)["USDT"][0], 100.0 - 0.1)


class InjectionTests(unittest.TestCase):
    def test_fail_next_has_no_side_effects_and_counts_down(self):
        ex = make()
        err = ApiResult(ok=False, kind="transient", error="boom")
        ex.fail_next("market_buy", err, times=2)
        self.assertIs(ex.market_buy("ABCUSDT", 1.0, "b1"), err)
        self.assertIs(ex.market_buy("ABCUSDT", 1.0, "b1"), err)
        self.assertEqual(bal(ex)["USDT"], (1000.0, 0.0))
        self.assertFalse(ex.get_order("ABCUSDT", "b1").value.exists)   # id not consumed
        self.assertTrue(ex.market_buy("ABCUSDT", 1.0, "b1").ok)

    def test_fail_next_only_affects_named_method(self):
        ex = make()
        ex.fail_next("balances", ApiResult(ok=False, kind="transient", error="x"))
        self.assertTrue(ex.market_buy("ABCUSDT", 1.0, "b1").ok)
        self.assertFalse(ex.balances().ok)
        self.assertTrue(ex.balances().ok)

    def test_fail_next_does_not_consume_fill_ratio(self):
        ex = make()
        ex.set_fill_ratio(0.5)
        ex.fail_next("market_buy", ApiResult(ok=False, kind="transient", error="x"))
        ex.market_buy("ABCUSDT", 1.0, "b1")
        self.assertEqual(ex.market_buy("ABCUSDT", 1.0, "b2").value.status, "EXPIRED")

    def test_drop_response_processes_but_reports_transient(self):
        ex = make()
        ex.drop_response_next("market_buy")
        r = ex.market_buy("ABCUSDT", 1.0, "b1")
        self.assertEqual((r.ok, r.kind, r.error), (False, "transient", "response lost"))
        self.assertAlmostEqual(bal(ex)["USDT"][0], 900.0)
        self.assertEqual(ex.get_order("ABCUSDT", "b1").value.status, "FILLED")
        self.assertTrue(ex.market_buy("ABCUSDT", 1.0, "b2").ok)       # one-shot

    def test_drop_response_on_place_stop(self):
        ex = make({"ABC": 5.0})
        ex.drop_response_next("place_stop")
        r = ex.place_stop("ABCUSDT", 1.0, 90.0, 89.0, "p1")
        self.assertEqual(r.kind, "transient")
        self.assertEqual(bal(ex)["ABC"], (4.0, 1.0))

    def test_calls_recorded_including_failures(self):
        ex = make()
        ex.fail_next("market_buy", ApiResult(ok=False, kind="transient", error="x"))
        ex.market_buy("ABCUSDT", 1.0, "b1")
        ex.market_buy("ABCUSDT", 1.0, "b1")
        ex.get_order("ABCUSDT", "b1")
        ex.open_orders()
        ex.cancel_order("ABCUSDT", "b1")
        ex.place_stop("ABCUSDT", 1.0, 90.0, 89.0, "p")
        ex.daily_candles("ABCUSDT", 3)
        ex.balances()
        ex.symbol_rules()
        ex.server_time()
        ex.market_sell("ABCUSDT", 1.0, "s")
        self.assertEqual([c[0] for c in ex.calls],
                         ["market_buy", "market_buy", "get_order", "open_orders", "cancel_order",
                          "place_stop", "daily_candles", "balances", "symbol_rules", "server_time",
                          "market_sell"])
        self.assertEqual(ex.calls[0][1], {"symbol": "ABCUSDT", "qty": 1.0, "client_id": "b1"})
        self.assertEqual(ex.calls[5][1], {"symbol": "ABCUSDT", "qty": 1.0, "stop_price": 90.0,
                                          "limit_price": 89.0, "client_id": "p"})
        self.assertEqual(ex.calls[3][1], {"symbol": None})

    def test_helpers_are_not_recorded(self):
        ex = make()
        ex.set_price("ABCUSDT", 1.0)
        ex.trigger_stops()
        self.assertEqual(ex.calls, [])


class FeedTests(unittest.TestCase):
    def test_quote_defaults_and_age(self):
        f = FakeFeed()
        f.set_quote("ABCUSDT", 10.0, volume=5.0, day="2026-11-17")
        q = f.quote("ABCUSDT")
        self.assertEqual((q.price, q.open, q.high, q.low, q.volume), (10.0, 10.0, 10.0, 10.0, 5.0))
        self.assertEqual(q.day, "2026-11-17")
        self.assertAlmostEqual(q.quote_volume, 50.0)

    def test_stale_quote_returns_none(self):
        f = FakeFeed()
        f.set_quote("ABCUSDT", 10.0, age=20.0)
        self.assertIsNone(f.quote("ABCUSDT", max_age=15))
        self.assertIsNotNone(f.quote("ABCUSDT", max_age=30))
        f.set_quote("ABCUSDT", 10.0, age=15.0)
        self.assertIsNotNone(f.quote("ABCUSDT", max_age=15))

    def test_received_at_is_age_seconds_before_feed_clock(self):
        f = FakeFeed()
        f.set_quote("ABCUSDT", 10.0, age=7.0)
        q = f.quote("ABCUSDT", max_age=100)
        self.assertAlmostEqual(f.now - q.received_at, 7.0)

    def test_unknown_symbol_none(self):
        self.assertIsNone(FakeFeed().quote("NOPE"))

    def test_health_and_records(self):
        f = FakeFeed()
        self.assertEqual(f.health(), (True, ""))
        f.set_health(False, "down")
        self.assertEqual(f.health(), (False, "down"))
        f.start()
        f.set_symbols(["A", "B"])
        f.set_symbols(iter(["C"]))
        self.assertEqual(f.symbols, ["C"])
        self.assertEqual(f.started, 1)


class PartialStopAndLagTests(unittest.TestCase):
    @mock.patch.object(config, "PROTECTIVE_STOP_TYPE", "STOP_LOSS_LIMIT")   # 지정가 체결을 고정하는 테스트
    def test_partial_trigger_keeps_remainder_locked_and_open(self):
        ex = make({"USDT": 0.0, "ABC": 5.0})
        ex.place_stop("ABCUSDT", 2.0, 90.0, 89.0, "p1")
        ex.set_price("ABCUSDT", 90.0)
        ex.trigger_stops(fill_ratio=0.4)
        o = ex.get_order("ABCUSDT", "p1").value
        self.assertEqual(o.status, "PARTIALLY_FILLED")
        self.assertAlmostEqual(o.executed_qty, 0.8)
        self.assertAlmostEqual(o.quote_qty, 0.8 * 89.0)
        self.assertAlmostEqual(o.fees["USDT"], 0.8 * 89.0 * 0.001)
        b = bal(ex)
        self.assertAlmostEqual(b["ABC"][0], 3.0)
        self.assertAlmostEqual(b["ABC"][1], 1.2)
        self.assertAlmostEqual(b["USDT"][0], 0.8 * 89.0 * 0.999)
        self.assertEqual([s.client_id for s in ex.open_orders().value], ["p1"])
        ex.trigger_stops()                          # 부분 체결된 주문은 다시 발동하지 않는다
        self.assertAlmostEqual(ex.get_order("ABCUSDT", "p1").value.executed_qty, 0.8)

    @mock.patch.object(config, "PROTECTIVE_STOP_TYPE", "STOP_LOSS_LIMIT")   # 지정가 체결을 고정하는 테스트
    def test_cancel_partial_unlocks_remainder_and_reports_no_fees(self):
        ex = make({"USDT": 0.0, "ABC": 5.0})
        ex.place_stop("ABCUSDT", 2.0, 90.0, 89.0, "p1")
        ex.set_price("ABCUSDT", 90.0)
        ex.trigger_stops(fill_ratio=0.4)
        r = ex.cancel_order("ABCUSDT", "p1")
        self.assertTrue(r.ok)
        self.assertEqual(r.value.status, "CANCELED")
        self.assertAlmostEqual(r.value.executed_qty, 0.8)
        self.assertEqual(r.value.fees, {})              # 실제 어댑터처럼: 취소 응답엔 수수료가 없다
        self.assertEqual(bal(ex)["ABC"], (4.2, 0.0))
        o = ex.get_order("ABCUSDT", "p1").value
        self.assertEqual(o.status, "CANCELED")
        self.assertAlmostEqual(o.fees["USDT"], 0.8 * 89.0 * 0.001)

    def test_hide_order_for_reports_unknown_then_visible(self):
        ex = make()
        ex.market_buy("ABCUSDT", 1.0, "b1")
        ex.hide_order_for(2)
        self.assertFalse(ex.get_order("ABCUSDT", "b1").value.exists)
        self.assertFalse(ex.get_order("ABCUSDT", "b1").value.exists)
        o = ex.get_order("ABCUSDT", "b1").value
        self.assertTrue(o.exists)
        self.assertEqual(o.status, "FILLED")


if __name__ == "__main__":
    unittest.main()
