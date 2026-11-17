import unittest
from datetime import timedelta
from unittest import mock

from coin_sniper import config
from coin_sniper.halts import DAILY_LOSS, MAINTENANCE, ORDER_UNCONFIRMED, PROTECTIVE_CANCEL
from coin_sniper.ledger import add_intent
from coin_sniper.models import ApiResult, OrderState, SymbolRules
from coin_sniper.risk import account_value

from helpers import DEFAULT_PRICES, add_position, make_engine, position, restart_engine, utc

T0 = utc(2026, 11, 17, 12)
SEC = timedelta(seconds=1)
HOUR = timedelta(hours=1)
SYM = "SOLUSDT"
TRANSIENT = ApiResult(False, kind="transient", error="HTTP 503")


class Crash(BaseException):
    """프로세스가 죽는 것을 흉내 낸다(엔진의 except Exception에 잡히지 않는다)."""


def codes(report) -> set[str]:
    return {h["code"] for h in report.halts}


def names(ex) -> list[str]:
    return [n for n, _ in ex.calls]


def calls(ex, name: str) -> list[dict]:
    return [p for n, p in ex.calls if n == name]


class EngineExitTests(unittest.TestCase):
    def build(self, price=100.0, qty=1.0, buy=100.0, symbol=SYM, rule_overrides=None, **fields):
        eng, ex, feed, store = make_engine(test=self, rule_overrides=rule_overrides)
        eng.startup(T0)
        add_position(eng, ex, symbol, qty, buy, **fields)
        self.price(ex, feed, price, symbol)
        return eng, ex, feed, store

    @staticmethod
    def price(ex, feed, value, symbol=SYM):
        ex.set_price(symbol, value)
        feed.set_quote(symbol, value)

    @staticmethod
    def trades(store) -> list[dict]:
        return store.load("trades", [])

    # ---- 봇 청산 -------------------------------------------------------------------
    def test_stop_hit_sells_at_market_and_records_net_pnl(self):
        eng, ex, feed, store = self.build(price=92.0, stop_loss_pct=0.07, stop_price=93.0)
        r = eng.run_once(T0)
        trades = self.trades(store)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["type"], t["symbol"], t["reason"]), ("SELL", SYM, "동적 손절"))
        self.assertAlmostEqual(t["qty"], 1.0)
        self.assertAlmostEqual(t["price"], 92.0)
        self.assertAlmostEqual(t["quote_qty"], 92.0)
        self.assertAlmostEqual(t["fees"]["USDT"], 0.092)
        self.assertAlmostEqual(t["entry_price"], 100.0)
        self.assertAlmostEqual(t["gross_pnl"], -8.0)
        self.assertAlmostEqual(t["net_pnl"], 92.0 - 0.092 - 100.0)
        self.assertAlmostEqual(t["net_return_pct"], (92.0 - 0.092 - 100.0) / 100.0 * 100)
        self.assertAlmostEqual(t["slippage_pct"], 0.0)
        self.assertTrue(t["counted"])
        self.assertEqual(t["dust_writeoff"], 0)
        self.assertEqual(t["strategy"], "vbo")
        self.assertEqual(t["time"], T0.isoformat())
        self.assertTrue(t["client_id"].startswith("cs-d-ex-"))
        self.assertAlmostEqual(store.load("risk_state", {})["realized_pnl"], t["net_pnl"])
        self.assertEqual(store.load("positions", {}), {})
        self.assertEqual(store.load("intents", {}), {})
        sells = [e for e in r.events if e["type"] == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertAlmostEqual(sells[0]["net_return_pct"], t["net_return_pct"])
        self.assertEqual(sells[0]["symbol"], SYM)

    def test_bot_exit_cancels_protective_order_before_selling(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        prot = store.load("positions", {})[SYM]["protective"]
        self.assertEqual(prot["state"], "open")
        self.price(ex, feed, 92.0)
        eng.run_once(T0 + 30 * SEC)
        seq = names(ex)
        self.assertIn("cancel_order", seq)
        self.assertLess(seq.index("cancel_order"), seq.index("market_sell"))
        self.assertEqual(calls(ex, "cancel_order")[0]["client_id"], prot["client_id"])
        self.assertEqual(len(self.trades(store)), 1)
        self.assertEqual(store.load("positions", {}), {})

    def test_protective_cancel_failure_blocks_sell_and_sets_halt(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        self.price(ex, feed, 92.0)
        ex.fail_next("cancel_order", TRANSIENT)
        r = eng.run_once(T0 + 30 * SEC)
        self.assertEqual(calls(ex, "market_sell"), [])
        self.assertIn(PROTECTIVE_CANCEL, codes(r))
        self.assertIn(SYM, [h for h in r.halts if h["code"] == PROTECTIVE_CANCEL][0]["detail"])
        self.assertIn(SYM, store.load("positions", {}))
        self.assertEqual(self.trades(store), [])
        r2 = eng.run_once(T0 + 60 * SEC)
        self.assertEqual(len(calls(ex, "market_sell")), 1)
        self.assertNotIn(PROTECTIVE_CANCEL, codes(r2))
        self.assertEqual(len(self.trades(store)), 1)

    def test_time_stop_exits_after_168_hours(self):
        entry = T0 - 168 * HOUR + 60 * SEC
        eng, ex, feed, store = self.build(entry_time=entry.isoformat())
        eng.run_once(T0)                                   # 167시간 59분: 유지
        self.assertEqual(calls(ex, "market_sell"), [])
        eng.run_once(T0 + 60 * SEC)                         # 168시간: 청산
        trades = self.trades(store)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "타임스탑 (168시간 경과)")
        self.assertEqual(store.load("positions", {}), {})

    def test_position_older_than_168h_exits_on_first_loop_after_restart(self):
        eng, ex, feed, store = make_engine(test=self, balances={"USDT": 10000.0, "SOL": 1.0})
        pos = position(SYM, 1.0, 100.0)
        pos["entry_time"] = (T0 - 240 * HOUR).isoformat()
        store.save("positions", {SYM: pos})
        eng.startup(T0)
        eng.run_once(T0)
        trades = self.trades(store)
        self.assertEqual([t["reason"] for t in trades], ["타임스탑 (168시간 경과)"])
        self.assertEqual(store.load("positions", {}), {})

    def test_protect_then_trail_stop_levels_are_persisted(self):
        eng, ex, feed, store = self.build()
        self.price(ex, feed, 107.0)
        eng.run_once(T0)
        pos = store.load("positions", {})[SYM]
        self.assertAlmostEqual(pos["stop_price"], 101.5)
        self.assertAlmostEqual(pos["highest_price"], 107.0)
        self.assertFalse(pos["trailing_active"])
        self.price(ex, feed, 113.0)
        eng.run_once(T0 + 30 * SEC)
        pos = store.load("positions", {})[SYM]
        self.assertAlmostEqual(pos["stop_price"], 113.0 * 0.95)
        self.assertTrue(pos["trailing_active"])
        self.price(ex, feed, 110.0)
        eng.run_once(T0 + 60 * SEC)
        pos = store.load("positions", {})[SYM]
        self.assertAlmostEqual(pos["stop_price"], 113.0 * 0.95)       # 내려가지 않는다
        self.assertAlmostEqual(pos["highest_price"], 113.0)
        self.assertEqual(calls(ex, "market_sell"), [])

    def test_protective_order_replaced_only_after_min_move(self):
        eng, ex, feed, store = self.build(stop_loss_pct=0.07, stop_price=93.0)
        eng.run_once(T0)
        first = store.load("positions", {})[SYM]["protective"]
        self.assertAlmostEqual(first["stop_price"], 93.0)
        eng.positions[SYM]["stop_price"] = 93.3              # 0.5% 미만 상승
        eng.run_once(T0 + 30 * SEC)
        self.assertEqual(len(calls(ex, "place_stop")), 1)
        self.assertEqual(calls(ex, "cancel_order"), [])
        self.assertEqual(store.load("positions", {})[SYM]["protective"]["client_id"], first["client_id"])
        self.price(ex, feed, 107.0)                         # 보호선 101.5
        eng.run_once(T0 + 60 * SEC)
        self.assertEqual([p["client_id"] for p in calls(ex, "cancel_order")], [first["client_id"]])
        places = calls(ex, "place_stop")
        self.assertEqual(len(places), 2)
        self.assertAlmostEqual(places[1]["stop_price"], 101.5)
        seq = names(ex)
        self.assertLess(seq.index("cancel_order"), len(seq) - 1 - seq[::-1].index("place_stop"))
        now = store.load("positions", {})[SYM]["protective"]
        self.assertEqual((now["state"], now["client_id"]), ("open", places[1]["client_id"]))

    def test_protective_prices_and_qty_are_floored_to_tick_and_step(self):
        sol = SymbolRules(symbol=SYM, base="SOL", quote="USDT", trading=True,
                          step_size=0.001, tick_size=0.01, min_notional=5.0)
        eng, ex, feed, store = self.build(qty=1.0005, stop_loss_pct=0.069813, stop_price=93.0187,
                                          rule_overrides={SYM: sol})
        eng.run_once(T0)
        place = calls(ex, "place_stop")[0]
        self.assertEqual(place["stop_price"], 93.01)
        self.assertEqual(place["limit_price"], 92.07)       # floor(93.01 × 0.99 = 92.0799)
        self.assertEqual(place["qty"], 1.0)
        prot = store.load("positions", {})[SYM]["protective"]
        self.assertEqual((prot["stop_price"], prot["limit_price"], prot["qty"]), (93.01, 92.07, 1.0))

    # ---- 거래소 손절 -----------------------------------------------------------------
    def test_exchange_stop_filled_first_is_recorded_as_sell(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        cid = store.load("positions", {})[SYM]["protective"]["client_id"]
        self.price(ex, feed, 94.0)
        ex.trigger_stops()
        eng.run_once(T0 + 30 * SEC)
        trades = self.trades(store)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["reason"], t["client_id"]), ("거래소 손절", cid))
        self.assertAlmostEqual(t["price"], 94.05)
        self.assertIsNone(t["slippage_pct"])
        self.assertAlmostEqual(t["net_pnl"], 94.05 * 0.999 - 100.0)
        self.assertEqual(store.load("positions", {}), {})
        self.assertEqual(calls(ex, "market_sell"), [])

    def test_exchange_stop_filled_during_bot_exit_is_recorded_once(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        cid = store.load("positions", {})[SYM]["protective"]["client_id"]
        self.price(ex, feed, 92.0)
        stale = OrderState(client_id=cid, symbol=SYM, side="SELL", status="NEW")
        ex.fail_next("get_order", ApiResult(True, value=stale))   # 확인 시점엔 아직 걸려 있었다
        ex.trigger_stops()                                  # 취소 직전에 거래소가 체결
        eng.run_once(T0 + 30 * SEC)
        trades = self.trades(store)
        self.assertEqual([t["reason"] for t in trades], ["거래소 손절"])
        self.assertEqual(calls(ex, "market_sell"), [])
        self.assertFalse(eng.halts.is_set(PROTECTIVE_CANCEL))
        self.assertEqual(store.load("positions", {}), {})

    def test_missing_protective_order_is_replaced_with_risk_event(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        cid = store.load("positions", {})[SYM]["protective"]["client_id"]
        ex.cancel_order(SYM, cid)                            # 잔고 잠금 해제
        del ex._orders[cid]                                  # 거래소가 모르는 주문
        r = eng.run_once(T0 + 30 * SEC)
        self.assertIn(("RISK", SYM, "보호 주문 없음: 재등록"),
                      [(e["type"], e["symbol"], e["reason"]) for e in r.events])
        prot = store.load("positions", {})[SYM]["protective"]
        self.assertEqual(prot["state"], "open")
        self.assertNotEqual(prot["client_id"], cid)
        self.assertEqual(len(calls(ex, "place_stop")), 2)

    def test_canceled_protective_order_is_replaced_with_risk_event(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        cid = store.load("positions", {})[SYM]["protective"]["client_id"]
        ex.cancel_order(SYM, cid)                            # 사람이 거래소에서 취소
        r = eng.run_once(T0 + 30 * SEC)
        self.assertIn("보호 주문 없음: 재등록", [e["reason"] for e in r.events if e["type"] == "RISK"])
        prot = store.load("positions", {})[SYM]["protective"]
        self.assertEqual(prot["state"], "open")
        self.assertNotEqual(prot["client_id"], cid)
        self.assertEqual(self.trades(store), [])

    def test_pending_protective_resolved_after_dropped_place_response(self):
        eng, ex, feed, store = self.build()
        ex.drop_response_next("place_stop")                  # 주문은 걸렸는데 응답이 사라짐
        eng.run_once(T0)
        prot = store.load("positions", {})[SYM]["protective"]
        self.assertEqual(prot["state"], "pending")
        eng.run_once(T0 + 30 * SEC)
        again = store.load("positions", {})[SYM]["protective"]
        self.assertEqual((again["state"], again["client_id"]), ("open", prot["client_id"]))
        self.assertEqual(len(calls(ex, "place_stop")), 1)

    def test_pending_protective_never_placed_is_replaced(self):
        eng, ex, feed, store = self.build()
        ex.fail_next("place_stop", TRANSIENT)                # 주문이 거래소에 닿지 않음
        eng.run_once(T0)
        prot = store.load("positions", {})[SYM]["protective"]
        self.assertEqual(prot["state"], "pending")
        r = eng.run_once(T0 + 30 * SEC)
        self.assertIn("보호 주문 없음: 재등록", [e["reason"] for e in r.events])
        again = store.load("positions", {})[SYM]["protective"]
        self.assertEqual(again["state"], "open")
        self.assertNotEqual(again["client_id"], prot["client_id"])

    def test_protective_stops_disabled_never_calls_place_stop(self):
        with mock.patch.object(config, "PROTECTIVE_STOPS_ENABLED", False):
            eng, ex, feed, store = self.build()
            eng.run_once(T0)
            self.assertIsNone(store.load("positions", {})[SYM]["protective"])
            self.price(ex, feed, 92.0)
            eng.run_once(T0 + 30 * SEC)
        self.assertEqual(calls(ex, "place_stop"), [])
        self.assertEqual(calls(ex, "cancel_order"), [])
        self.assertEqual(len(self.trades(store)), 1)

    # ---- 부분 체결과 잔량 --------------------------------------------------------------
    def test_partial_sell_keeps_remainder_and_retries(self):
        eng, ex, feed, store = self.build(price=92.0)
        ex.set_fill_ratio(0.4)
        eng.run_once(T0)
        trades = self.trades(store)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["qty"], 0.4)
        self.assertAlmostEqual(trades[0]["net_pnl"], 0.4 * 92 * 0.999 - 40.0)
        pos = store.load("positions", {})[SYM]
        self.assertAlmostEqual(pos["qty"], 0.6)
        self.assertAlmostEqual(pos["cost"], 60.0)
        self.assertEqual(eng.metrics.summary(T0, 1)["orders_partial"], 1)
        eng.run_once(T0 + 30 * SEC)
        trades = self.trades(store)
        self.assertEqual(len(trades), 2)
        self.assertAlmostEqual(trades[1]["qty"], 0.6)
        self.assertEqual(store.load("positions", {}), {})
        total = sum(t["net_pnl"] for t in trades)
        self.assertAlmostEqual(store.load("risk_state", {})["realized_pnl"], total)

    def test_remainder_below_min_notional_becomes_dust(self):
        eng, ex, feed, store = self.build(price=92.0)
        cash_before, _ = account_value(eng.risk_state, eng.positions, {SYM: 92.0})
        usdt_before = ex.balances().value["USDT"][0]
        ex.set_fill_ratio(0.95)                              # 남은 0.05 × 92 = 4.6 < 5
        r = eng.run_once(T0)
        self.assertEqual(store.load("positions", {}), {})
        dust = store.load("dust", [])
        self.assertEqual(len(dust), 1)
        self.assertEqual((dust[0]["symbol"], dust[0]["time"]), (SYM, T0.isoformat()))
        self.assertAlmostEqual(dust[0]["qty"], 0.05)
        t = self.trades(store)[0]
        self.assertAlmostEqual(t["dust_writeoff"], 5.0)
        self.assertAlmostEqual(t["net_pnl"], 0.95 * 92 * 0.999 - 100.0)
        # 가상 현금은 실제로 들어온 돈만큼만 늘어난다(잔량 원가는 손실로 처리)
        proceeds = ex.balances().value["USDT"][0] - usdt_before
        self.assertAlmostEqual(r.cash, cash_before + proceeds)
        self.assertAlmostEqual(store.load("risk_state", {})["realized_pnl"], t["net_pnl"])

    # ---- 확인되지 않은 주문과 의도 복구 ---------------------------------------------------
    def test_unconfirmed_sell_keeps_position_and_sets_halt(self):
        eng, ex, feed, store = self.build(price=92.0)
        sleeps: list[float] = []
        eng.sleep = sleeps.append
        ex.fail_next("get_order", TRANSIENT, times=config.ORDER_CONFIRM_TIMEOUT_SECONDS)
        r = eng.run_once(T0)
        self.assertIn(ORDER_UNCONFIRMED, codes(r))
        self.assertFalse(r.entry_allowed)
        self.assertAlmostEqual(store.load("positions", {})[SYM]["qty"], 1.0)
        self.assertEqual(len(store.load("intents", {})), 1)
        self.assertEqual(self.trades(store), [])
        self.assertEqual(len(calls(ex, "get_order")), config.ORDER_CONFIRM_TIMEOUT_SECONDS)
        self.assertEqual(sleeps.count(1), config.ORDER_CONFIRM_TIMEOUT_SECONDS - 1)
        self.assertEqual(eng.metrics.summary(T0, 1)["orders_unconfirmed"], 1)
        r2 = eng.run_once(T0 + 30 * SEC)                    # 다음 루프에 조회 성공 → 반영
        self.assertNotIn(ORDER_UNCONFIRMED, codes(r2))
        self.assertEqual(len(self.trades(store)), 1)
        self.assertEqual(store.load("intents", {}), {})
        self.assertEqual(len(calls(ex, "market_sell")), 1)  # 다시 보내지 않는다

    def test_no_new_sell_while_previous_sell_is_unconfirmed(self):
        eng, ex, feed, store = self.build(price=92.0)
        ex.fail_next("get_order", TRANSIENT, times=config.ORDER_CONFIRM_TIMEOUT_SECONDS + 1)
        eng.run_once(T0)
        r = eng.run_once(T0 + 30 * SEC)                     # 1단계 조회도 실패: 결과를 여전히 모른다
        self.assertIn(ORDER_UNCONFIRMED, codes(r))
        self.assertEqual(len(calls(ex, "market_sell")), 1)  # 손절 조건이어도 다시 팔지 않는다
        self.assertEqual(len(store.load("intents", {})), 1)

    def test_pending_intent_resolved_after_crash(self):
        eng, ex, feed, store = self.build(price=92.0)
        ex.drop_response_next("market_sell")                 # 체결은 됐는데 응답이 사라짐
        ex.fail_next("get_order", TRANSIENT)

        def die(_s):
            raise Crash()
        eng.sleep = die                                      # 확인 중에 프로세스가 죽는다
        with self.assertRaises(Crash):
            eng.run_once(T0)
        self.assertEqual(len(store.load("intents", {})), 1)  # 보내기 전에 저장됐다
        eng2 = restart_engine(eng, T0 + 30 * SEC)
        eng2.run_once(T0 + 30 * SEC)
        trades = self.trades(store)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "동적 손절")
        self.assertEqual(store.load("intents", {}), {})
        self.assertEqual(store.load("positions", {}), {})
        self.assertEqual(len(calls(ex, "market_sell")), 1)

    def test_fill_applied_once_when_crash_before_intent_removed(self):
        eng, ex, feed, store = self.build(price=92.0)
        real_save = store.save

        def save(name, data):
            if name == "risk_state":                         # 체결 기록·포지션 저장 뒤, 의도 삭제 전
                raise Crash()
            real_save(name, data)
        store.save = save
        with self.assertRaises(Crash):
            eng.run_once(T0)
        store.save = real_save
        self.assertEqual(len(self.trades(store)), 1)
        self.assertEqual(len(store.load("intents", {})), 1)
        eng2 = restart_engine(eng, T0 + 30 * SEC)
        eng2.run_once(T0 + 30 * SEC)
        trades = self.trades(store)
        self.assertEqual(len(trades), 1)
        self.assertEqual(store.load("intents", {}), {})
        self.assertEqual(store.load("positions", {}), {})
        self.assertAlmostEqual(store.load("risk_state", {})["realized_pnl"], trades[0]["net_pnl"])
        self.assertEqual(len(calls(ex, "market_sell")), 1)

    def test_intent_for_order_exchange_never_saw_is_dropped(self):
        eng, ex, feed, store = make_engine(test=self)
        intents = add_intent({}, "cs-d-ex-000000000000", SYM, "SELL", 1.0, "exit", "동적 손절", T0)
        store.save("intents", intents)
        eng.startup(T0)
        add_position(eng, ex, SYM, 1.0, 100.0)
        r = eng.run_once(T0)                                # 아직 조회 지연일 수 있다: 모름
        self.assertEqual(len(store.load("intents", {})), 1)
        self.assertIn(ORDER_UNCONFIRMED, codes(r))
        grace = config.INTENT_NOT_FOUND_GRACE_SECONDS
        r2 = eng.run_once(T0 + grace * SEC)                 # 유예가 지나면 "주문된 적 없음"
        self.assertEqual(store.load("intents", {}), {})
        self.assertAlmostEqual(store.load("positions", {})[SYM]["qty"], 1.0)
        self.assertEqual(self.trades(store), [])
        self.assertNotIn(ORDER_UNCONFIRMED, codes(r2))
        self.assertNotIn(MAINTENANCE, codes(r2))
        self.assertEqual([e for e in r2.events if e["type"] == "RISK"], [])

    # ---- 보류와 중단 ------------------------------------------------------------------
    def test_exit_skipped_when_quote_missing(self):
        eng, ex, feed, store = self.build()
        ex.set_price(SYM, 90.0)
        feed.set_quote(SYM, 90.0, age=config.QUOTE_MAX_AGE_SECONDS + 1)
        r = eng.run_once(T0)
        self.assertEqual(calls(ex, "market_sell"), [])
        self.assertIn(("RISK", SYM, "실시간 시세 없음: 청산 판단 보류"),
                      [(e["type"], e["symbol"], e["reason"]) for e in r.events])
        self.assertIn(SYM, store.load("positions", {}))

    def test_untradable_symbol_rejection_does_not_spam(self):
        prices = dict(DEFAULT_PRICES, XUSDT=100.0)
        eng, ex, feed, store = make_engine(prices=prices, test=self)
        eng.startup(T0)
        add_position(eng, ex, "XUSDT", 1.0, 100.0)
        add_position(eng, ex, SYM, 1.0, 100.0)
        self.price(ex, feed, 90.0, "XUSDT")
        self.price(ex, feed, 92.0, SYM)
        ex.set_trading("XUSDT", False)
        for i in range(5):
            r = eng.run_once(T0 + 30 * i * SEC)
            self.assertNotIn(ORDER_UNCONFIRMED, codes(r))
            self.assertNotIn(MAINTENANCE, codes(r))
        risk = [e for e in store.load("events", []) if e["type"] == "RISK" and e["symbol"] == "XUSDT"]
        self.assertEqual(len(risk), 1)
        self.assertEqual(risk[0]["count"], 5)
        self.assertTrue(risk[0]["reason"].startswith("매도 거절: "))
        self.assertIn("XUSDT", store.load("positions", {}))
        self.assertNotIn(SYM, store.load("positions", {}))    # 다른 종목 청산은 막히지 않는다

    def test_exits_run_even_when_entry_halted(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        eng.halts.set(DAILY_LOSS, "일일 손실 -5.00%", T0)
        self.price(ex, feed, 92.0)
        r = eng.run_once(T0 + 30 * SEC)
        self.assertIn(DAILY_LOSS, codes(r))
        self.assertEqual(len(self.trades(store)), 1)
        self.assertEqual(store.load("positions", {}), {})

    def test_order_refused_during_maintenance_sets_halt_and_retries_later(self):
        eng, ex, feed, store = self.build(price=92.0)
        ex.fail_next("market_sell", TRANSIENT)
        r = eng.run_once(T0)
        self.assertIn(MAINTENANCE, codes(r))                 # 실패 즉시
        self.assertIn(ORDER_UNCONFIRMED, codes(r))           # 의도는 유예 동안 남는다
        self.assertEqual(len(store.load("intents", {})), 1)
        self.assertIn(SYM, store.load("positions", {}))
        self.assertEqual(self.trades(store), [])
        r2 = eng.run_once(T0 + 30 * SEC)                    # 계좌 조회 성공 → MAINTENANCE 해제, 의도는 아직
        self.assertNotIn(MAINTENANCE, codes(r2))
        self.assertIn(ORDER_UNCONFIRMED, codes(r2))
        self.assertEqual(len(calls(ex, "market_sell")), 1)
        r3 = eng.run_once(T0 + config.INTENT_NOT_FOUND_GRACE_SECONDS * SEC)
        self.assertNotIn(ORDER_UNCONFIRMED, codes(r3))
        self.assertEqual(store.load("intents", {}), {})
        self.assertEqual(len(calls(ex, "market_sell")), 2)   # 유예 뒤 재시도
        self.assertEqual(len(self.trades(store)), 1)
        self.assertEqual(store.load("positions", {}), {})
        self.assertEqual(eng.metrics.summary(T0, 1)["orders_unconfirmed"], 1)   # 의도당 한 번

    def test_order_lookup_lag_is_unknown_not_absent(self):
        eng, ex, feed, store = self.build(price=92.0)
        ex.hide_order_for(config.ORDER_CONFIRM_TIMEOUT_SECONDS + 1)   # 체결됐지만 조회가 늦다
        r1 = eng.run_once(T0)
        self.assertIn(ORDER_UNCONFIRMED, codes(r1))
        self.assertEqual(len(store.load("intents", {})), 1)
        r2 = eng.run_once(T0 + 30 * SEC)                    # 아직 안 보인다: 종목은 바쁨, 다시 팔지 않는다
        self.assertIn(ORDER_UNCONFIRMED, codes(r2))
        self.assertEqual(len(calls(ex, "market_sell")), 1)
        self.assertEqual(self.trades(store), [])
        r3 = eng.run_once(T0 + 60 * SEC)                    # 보인다: FILLED → 한 번 반영
        self.assertNotIn(ORDER_UNCONFIRMED, codes(r3))
        self.assertEqual(len(self.trades(store)), 1)
        self.assertAlmostEqual(self.trades(store)[0]["fees"]["USDT"], 0.092)
        self.assertEqual(len(calls(ex, "market_sell")), 1)
        self.assertEqual(store.load("positions", {}), {})
        self.assertEqual(eng.metrics.summary(T0, 1)["orders_unconfirmed"], 1)

    # ---- 보호 주문의 부분 체결 (fix round 1) ------------------------------------------------
    def partially_stopped(self):
        """보호 주문이 0.4만 체결된 상태(가격 92). 반환: (eng, ex, feed, store, cid, usdt_before, cash_before)."""
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        cid = store.load("positions", {})[SYM]["protective"]["client_id"]
        cash_before, _ = account_value(eng.risk_state, eng.positions, {SYM: 100.0})
        usdt_before = ex.balances().value["USDT"][0]
        self.price(ex, feed, 92.0)
        ex.trigger_stops(fill_ratio=0.4)
        return eng, ex, feed, store, cid, usdt_before, cash_before

    def check_partial_then_sell(self, ex, store, r, usdt_before, cash_before):
        trades = self.trades(store)
        self.assertEqual([t["reason"] for t in trades], ["거래소 손절(부분)", "동적 손절"])
        part = trades[0]
        self.assertAlmostEqual(part["qty"], 0.4)
        self.assertAlmostEqual(part["fees"]["USDT"], 0.4 * 94.05 * 0.001)
        self.assertAlmostEqual(part["net_pnl"], 0.4 * 94.05 * 0.999 - 40.0)   # 실제 수수료 반영
        self.assertAlmostEqual(trades[1]["qty"], 0.6)
        self.assertEqual(len(calls(ex, "market_sell")), 1)
        self.assertEqual(len(calls(ex, "cancel_order")), 1)   # 다시 취소하지 않는다
        self.assertEqual(store.load("positions", {}), {})
        received = ex.balances().value["USDT"][0] - usdt_before
        self.assertAlmostEqual(r.cash, cash_before + received)

    def test_bot_exit_with_partially_filled_stop_records_real_fee_then_sells_rest(self):
        eng, ex, feed, store, cid, usdt_before, cash_before = self.partially_stopped()
        r = eng.run_once(T0 + 30 * SEC)
        self.check_partial_then_sell(ex, store, r, usdt_before, cash_before)

    def assert_unsettled(self, ex, store, r):
        """취소된 보호 주문의 최종 상태를 모르는 동안: 중단, 진입 불가, 매도 없음, 기록 유지, 반영 없음."""
        self.assertIn(PROTECTIVE_CANCEL, codes(r))
        self.assertIn(SYM, [h for h in r.halts if h["code"] == PROTECTIVE_CANCEL][0]["detail"])
        self.assertFalse(r.entry_allowed)
        self.assertEqual(calls(ex, "market_sell"), [])
        self.assertEqual(store.load("positions", {})[SYM]["protective"]["state"], "canceled")
        self.assertEqual(self.trades(store), [])
        self.assertEqual(calls(ex, "place_stop")[1:], [])    # 새 보호 주문도 걸지 않는다

    def test_lookup_failure_after_cancel_keeps_halt_until_settled(self):
        eng, ex, feed, store, cid, usdt_before, cash_before = self.partially_stopped()
        seen = ex.get_order(SYM, cid).value                  # 1단계 확인 때 돌려줄 상태(부분 체결 중)
        ex.fail_next("get_order", ApiResult(True, value=seen))
        ex.fail_next("get_order", TRANSIENT, times=3)        # 취소 직후부터 세 루프 내내 조회 실패
        for i in (1, 2, 3):
            r = eng.run_once(T0 + 30 * i * SEC)
            self.assert_unsettled(ex, store, r)
        r = eng.run_once(T0 + 120 * SEC)                     # 조회 회복 → 부분 체결 한 번, 나머지 매도
        self.check_partial_then_sell(ex, store, r, usdt_before, cash_before)
        self.assertNotIn(PROTECTIVE_CANCEL, codes(r))

    def test_canceled_record_not_settled_by_missing_lookup(self):
        eng, ex, feed, store, cid, usdt_before, cash_before = self.partially_stopped()
        seen = ex.get_order(SYM, cid).value
        ex.fail_next("get_order", ApiResult(True, value=seen))
        ex.hide_order_for(1)                                 # 취소 직후 조회: 거래소가 모른다고 답함
        r = eng.run_once(T0 + 30 * SEC)
        self.assert_unsettled(ex, store, r)
        r = eng.run_once(T0 + 30 * SEC + config.INTENT_NOT_FOUND_GRACE_SECONDS * SEC)
        self.check_partial_then_sell(ex, store, r, usdt_before, cash_before)
        self.assertNotIn(PROTECTIVE_CANCEL, codes(r))

    def test_canceled_record_not_settled_by_non_final_lookup(self):
        eng, ex, feed, store, cid, usdt_before, cash_before = self.partially_stopped()
        seen = ex.get_order(SYM, cid).value                  # PARTIALLY_FILLED (최종 아님)
        ex.fail_next("get_order", ApiResult(True, value=seen), times=2)   # 취소 뒤에도 늦은 상태가 보인다
        r = eng.run_once(T0 + 30 * SEC)
        self.assert_unsettled(ex, store, r)
        r = eng.run_once(T0 + 60 * SEC)
        self.check_partial_then_sell(ex, store, r, usdt_before, cash_before)
        self.assertNotIn(PROTECTIVE_CANCEL, codes(r))

    def test_cancel_failure_halt_kept_when_position_skipped_for_missing_quote(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        self.price(ex, feed, 92.0)
        ex.fail_next("cancel_order", TRANSIENT)
        self.assertIn(PROTECTIVE_CANCEL, codes(eng.run_once(T0 + 30 * SEC)))
        feed.set_quote(SYM, 92.0, age=config.QUOTE_MAX_AGE_SECONDS + 1)
        r = eng.run_once(T0 + 60 * SEC)                      # 시세가 없어 건너뜀: 해결된 게 아니다
        self.assertIn(PROTECTIVE_CANCEL, codes(r))
        self.assertEqual(calls(ex, "market_sell"), [])
        feed.set_quote(SYM, 92.0)
        r = eng.run_once(T0 + 90 * SEC)
        self.assertNotIn(PROTECTIVE_CANCEL, codes(r))
        self.assertEqual(len(self.trades(store)), 1)

    def test_unsettled_canceled_record_does_not_block_other_exits(self):
        eng, ex, feed, store, cid, usdt_before, cash_before = self.partially_stopped()
        add_position(eng, ex, "XRPUSDT", 10.0, 3.0)          # SOL 뒤에 처리된다. 손절 2.85, 현재가 2.0
        self.price(ex, feed, 2.0, "XRPUSDT")
        seen = ex.get_order(SYM, cid).value
        ex.fail_next("get_order", ApiResult(True, value=seen))
        ex.fail_next("get_order", TRANSIENT)
        r = eng.run_once(T0 + 30 * SEC)
        self.assertIn(PROTECTIVE_CANCEL, codes(r))
        self.assertEqual([t["symbol"] for t in self.trades(store)], ["XRPUSDT"])
        self.assertEqual([p["symbol"] for p in calls(ex, "market_sell")], ["XRPUSDT"])
        self.assertEqual(store.load("positions", {})[SYM]["protective"]["state"], "canceled")

    def test_crash_between_cancel_and_partial_journal_applies_fill_once(self):
        eng, ex, feed, store, cid, usdt_before, cash_before = self.partially_stopped()
        real_save = store.save

        def save(name, data):
            if name == "intents":                            # 저널 저장 직전에 죽는다
                raise Crash()
            real_save(name, data)
        store.save = save
        with self.assertRaises(Crash):
            eng.run_once(T0 + 30 * SEC)
        store.save = real_save
        self.assertEqual(store.load("positions", {})[SYM]["protective"]["state"], "canceled")
        eng2 = restart_engine(eng, T0 + 60 * SEC)
        r = eng2.run_once(T0 + 60 * SEC)
        self.check_partial_then_sell(ex, store, r, usdt_before, cash_before)

    # ---- 규칙 미확보와 잔량 (fix round 1) ---------------------------------------------------
    def test_intent_waits_for_rules_and_remainder_becomes_dust(self):
        eng, ex, feed, store = self.build(price=92.0, qty=1.0005)
        cash_before, _ = account_value(eng.risk_state, eng.positions, {SYM: 92.0})
        usdt_before = ex.balances().value["USDT"][0]
        cid = "cs-d-ex-aaaaaaaaaaaa"
        self.assertTrue(ex.market_sell(SYM, 1.0, cid).ok)    # 보냈고 체결됐는데 프로세스가 죽었다
        add_intent(eng.intents, cid, SYM, "SELL", 1.0, "exit", "동적 손절", T0)
        eng.intents[cid].update(ref_price=92.0, extra={})
        store.save("intents", eng.intents)
        ex.fail_next("symbol_rules", TRANSIENT, times=2)     # 의도 해소용 + 감시 종목 선정용
        eng2 = restart_engine(eng, T0)
        r1 = eng2.run_once(T0)
        self.assertEqual(eng2.rules, {})
        self.assertIn(ORDER_UNCONFIRMED, codes(r1))
        self.assertIn("규칙", [h for h in r1.halts if h["code"] == ORDER_UNCONFIRMED][0]["detail"])
        self.assertEqual(len(store.load("intents", {})), 1)
        self.assertAlmostEqual(store.load("positions", {})[SYM]["qty"], 1.0005)
        self.assertEqual(self.trades(store), [])
        r2 = eng2.run_once(T0 + config.UNIVERSE_RETRY_SECONDS * SEC)   # 규칙 재시도 간격 뒤
        self.assertNotIn(ORDER_UNCONFIRMED, codes(r2))
        trades = self.trades(store)
        self.assertEqual(len(trades), 1)
        self.assertEqual(store.load("intents", {}), {})
        self.assertEqual(store.load("positions", {}), {})
        dust = store.load("dust", [])
        self.assertEqual(len(dust), 1)
        self.assertAlmostEqual(dust[0]["qty"], 0.0005)
        received = ex.balances().value["USDT"][0] - usdt_before
        self.assertAlmostEqual(r2.cash, cash_before + received)

    def test_sub_step_position_is_closed_as_dust_with_event(self):
        eng, ex, feed, store = self.build(qty=0.0005)
        r = eng.run_once(T0)
        self.assertEqual(store.load("positions", {}), {})
        trades = self.trades(store)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["type"], t["qty"], t["reason"], t["counted"]), ("SELL", 0, "잔량 정리", False))
        self.assertAlmostEqual(t["dust_writeoff"], 0.05)
        self.assertAlmostEqual(store.load("risk_state", {})["realized_pnl"], -0.05)
        self.assertAlmostEqual(store.load("dust", [])[0]["qty"], 0.0005)
        self.assertIn(("RISK", SYM), [(e["type"], e["symbol"]) for e in r.events])
        self.assertEqual(calls(ex, "market_sell"), [])
        self.assertEqual(calls(ex, "place_stop"), [])
        self.assertAlmostEqual(r.cash, 1000.0 - 0.05)

    def test_exit_without_symbol_rules_sends_nothing(self):
        prices = dict(DEFAULT_PRICES)
        eng, ex, feed, store = make_engine(prices=prices, test=self)
        eng.startup(T0)
        add_position(eng, ex, "ZZZUSDT", 1.0, 100.0)         # 거래소 규칙에 없는 종목
        feed.set_quote("ZZZUSDT", 90.0)
        r = eng.run_once(T0)
        self.assertNotIn("ZZZUSDT", eng.rules)
        self.assertEqual(calls(ex, "market_sell"), [])
        self.assertEqual(calls(ex, "place_stop"), [])
        self.assertIn(("RISK", "ZZZUSDT"), [(e["type"], e["symbol"]) for e in r.events])
        self.assertIn("ZZZUSDT", store.load("positions", {}))


class ThirdAssetFeeTests(unittest.TestCase):
    """BNB 같은 제3 자산으로 낸 수수료를 USDT로 값을 매겨 원가·대금·순손익·가상 현금에 넣는다 (F4)."""
    BNB = 600.0

    def build(self, price=92.0, bnb_quote=True):
        eng, ex, feed, store = make_engine(test=self)
        eng.startup(T0)
        add_position(eng, ex, SYM, 1.0, 100.0)             # 원가 100, 손절선 95
        ex._account("BNB")[0] += 1
        ex.set_fee_asset("BNB", self.BNB)
        ex.set_price(SYM, price)
        feed.set_quote(SYM, price)
        if bnb_quote:
            feed.set_quote("BNBUSDT", self.BNB)
        return eng, ex, feed, store

    @staticmethod
    def usdt(ex) -> float:
        return float(ex._account("USDT")[0])

    def test_sell_with_bnb_fee_values_fee_in_proceeds_and_cash(self):
        eng, ex, feed, store = self.build(price=100.0)
        eng.run_once(T0 - 30 * SEC)                           # 가격이 손절선 위일 때 상태를 맞춘다
        ex.set_price(SYM, 92.0)
        feed.set_quote(SYM, 92.0)
        cash_before = account_value(eng.risk_state, eng.positions, {s: 0.0 for s in eng.positions})[0]
        usdt_before = self.usdt(ex)
        eng.run_once(T0)
        t = store.load("trades", [])[-1]
        fee_value = 92.0 * 0.001
        self.assertEqual(t["fees"], {"BNB": unittest.mock.ANY})
        self.assertAlmostEqual(t["fees"]["BNB"] * self.BNB, fee_value)
        self.assertAlmostEqual(t["fee_other_usdt"], fee_value)
        self.assertAlmostEqual(t["net_pnl"], 92.0 - fee_value - 100.0)
        cash_after = account_value(eng.risk_state, eng.positions, {s: 0.0 for s in eng.positions})[0]
        self.assertAlmostEqual(cash_after - cash_before, (self.usdt(ex) - usdt_before) - fee_value)

    def test_buy_with_bnb_fee_adds_valued_fee_to_cost(self):
        eng, ex, feed, store = self.build(price=100.0)
        eng.run_once(T0)                                      # 규칙 확보
        cash_before = account_value(eng.risk_state, eng.positions, {s: 0.0 for s in eng.positions})[0]
        usdt_before = self.usdt(ex)
        extra = {"stop_loss_pct": 0.05, "trailing_gap_pct": 0.05, "strategy": "vbo"}
        out = eng.executor.execute_market("XRPUSDT", "BUY", 50.0, "entry", "vbo", T0, ref_price=2.0, extra=extra)
        self.assertEqual(out.status, "filled")
        pos = eng.positions["XRPUSDT"]
        fee_value = 100.0 * 0.001
        self.assertAlmostEqual(pos["qty"], 50.0)              # 기초 자산 수수료 없음
        self.assertAlmostEqual(pos["cost"], 100.0 + fee_value)
        t = store.load("trades", [])[-1]
        self.assertAlmostEqual(t["fee_other_usdt"], fee_value)
        cash_after = account_value(eng.risk_state, eng.positions, {s: 0.0 for s in eng.positions})[0]
        self.assertAlmostEqual(cash_after - cash_before, (self.usdt(ex) - usdt_before) - fee_value)

    def test_unpriced_fee_asset_keeps_intent_then_journals_once_when_price_known(self):
        eng, ex, feed, store = self.build(bnb_quote=False)    # BNB 시세도 일봉도 없다
        r1 = eng.run_once(T0)
        self.assertEqual(len(calls(ex, "market_sell")), 1)
        self.assertEqual(store.load("trades", []), [])         # 값을 모르는 수수료로 반영하지 않는다
        self.assertIn(SYM, store.load("positions", {}))
        self.assertEqual(len(store.load("intents", {})), 1)
        self.assertIn(ORDER_UNCONFIRMED, codes(r1))
        ex.set_candles("BNBUSDT", [{"date": "2026-11-17", "open": 590.0, "high": 610.0, "low": 580.0,
                                    "close": self.BNB, "vol": 1.0, "quote_vol": 600.0}])
        r2 = eng.run_once(T0 + 30 * SEC)                      # 일봉 종가로 값을 매겨 반영
        trades = store.load("trades", [])
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["fee_other_usdt"], 92.0 * 0.001)
        self.assertEqual(store.load("intents", {}), {})
        self.assertNotIn(SYM, store.load("positions", {}))
        self.assertNotIn(ORDER_UNCONFIRMED, codes(r2))
        self.assertEqual(len(calls(ex, "market_sell")), 1)     # 다시 팔지 않는다
        eng.run_once(T0 + 60 * SEC)
        self.assertEqual(len(store.load("trades", [])), 1)

    def test_protective_fill_with_unpriced_fee_waits_without_selling(self):
        eng, ex, feed, store = self.build(price=100.0, bnb_quote=False)
        ex.set_fee_asset(None)
        eng.run_once(T0)                                      # 보호 주문 등록
        cid = eng.positions[SYM]["protective"]["client_id"]
        ex.set_price(SYM, 90.0)
        feed.set_quote(SYM, 90.0)
        ex.trigger_stops()
        ex._orders[cid].state.fees = {"BNB": 0.0001}          # 거래소가 BNB로 수수료를 받았다
        r1 = eng.run_once(T0 + 30 * SEC)
        self.assertEqual(store.load("trades", []), [])
        self.assertIn(cid, store.load("intents", {}))
        self.assertIn(ORDER_UNCONFIRMED, codes(r1))
        self.assertEqual(calls(ex, "market_sell"), [])         # 장부 수량을 믿고 팔지 않는다
        feed.set_quote("BNBUSDT", self.BNB)
        eng.run_once(T0 + 60 * SEC)
        trades = store.load("trades", [])
        self.assertEqual([t["client_id"] for t in trades], [cid])
        self.assertAlmostEqual(trades[0]["fee_other_usdt"], 0.0001 * self.BNB)
        self.assertNotIn(SYM, store.load("positions", {}))
        self.assertEqual(calls(ex, "market_sell"), [])


class FakeClock:
    """가짜 단조 시계: sleep이 시간을 흘려보낸다."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class ConfirmDeadlineTests(unittest.TestCase):
    """주문 확인은 시도 횟수가 아니라 단조 시계 기한으로 끝난다 (F6)."""

    def build(self):
        clock = FakeClock()
        patcher = mock.patch.object(config, "PROTECTIVE_STOPS_ENABLED", False)   # 수량이 잠기지 않게
        patcher.start()
        self.addCleanup(patcher.stop)
        eng, ex, feed, store = make_engine(test=self)
        eng.sleep, eng.monotonic = clock.sleep, clock.monotonic
        eng.startup(T0)
        add_position(eng, ex, SYM, 1.0, 100.0)
        ex.set_price(SYM, 100.0)
        feed.set_quote(SYM, 100.0)
        eng.run_once(T0)                                      # 규칙·보호 주문
        return eng, ex, feed, store, clock

    def test_slow_order_lookups_stop_at_deadline_and_keep_intent(self):
        eng, ex, feed, store, clock = self.build()
        real = ex.get_order

        def slow_hidden(symbol, client_id):                  # 한 번에 10초 걸리고 아직 안 보인다
            clock.t += 10
            ex.hide_order_for(1)
            return real(symbol, client_id)
        ex.get_order = slow_hidden
        start, n = clock.t, len(calls(ex, "get_order"))
        out = eng.executor.execute_market(SYM, "SELL", 0.5, "exit", "test", T0 + 30 * SEC, ref_price=100.0)
        self.assertEqual(out.status, "unconfirmed")
        self.assertLessEqual(clock.t - start, config.ORDER_CONFIRM_TIMEOUT_SECONDS + 10)
        self.assertEqual(len(calls(ex, "get_order")) - n, 2)  # 0초, 11초에 조회하고 21초에 멈춘다
        self.assertIn(out.client_id, store.load("intents", {}))
        self.assertTrue(eng.halts.is_set(ORDER_UNCONFIRMED))

    def test_transient_send_failure_polls_at_most_five_seconds(self):
        eng, ex, feed, store, clock = self.build()
        ex.fail_next("market_sell", TRANSIENT)                # 주문은 거래소에 없다(결과 불명)
        start, n = clock.t, len(calls(ex, "get_order"))
        out = eng.executor.execute_market(SYM, "SELL", 0.5, "exit", "test", T0 + 30 * SEC, ref_price=100.0)
        self.assertEqual(out.status, "unconfirmed")
        self.assertLessEqual(clock.t - start, config.ORDER_CONFIRM_AFTER_SEND_FAILURE_SECONDS)
        self.assertLessEqual(len(calls(ex, "get_order")) - n, config.ORDER_CONFIRM_AFTER_SEND_FAILURE_SECONDS)
        self.assertIn(out.client_id, store.load("intents", {}))

    def test_intent_created_at_is_the_actual_send_time(self):
        eng, ex, feed, store, clock = self.build()
        now = T0 + 30 * SEC
        eng.run_once(now)                                     # 루프 시작 시각(단조)을 정한다
        clock.t += 50                                         # 같은 루프 안에서 50초가 흘렀다
        ex.hide_order_for(100)
        out = eng.executor.execute_market(SYM, "SELL", 0.5, "exit", "test", now, ref_price=100.0)
        created = store.load("intents", {})[out.client_id]["created_at"]
        self.assertEqual(created, (now + 50 * SEC).isoformat())


if __name__ == "__main__":
    unittest.main()
