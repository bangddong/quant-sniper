import dataclasses
import unittest
from datetime import timedelta
from unittest import mock

from coin_sniper import config
from coin_sniper.halts import FEED_STALE, ORDER_UNCONFIRMED
from coin_sniper.ledger import add_intent
from coin_sniper.models import ApiResult, SymbolRules
from coin_sniper.risk import account_value

from helpers import add_position, make_engine, restart_engine, set_signal, trending_candles, utc

T0 = utc(2026, 11, 17, 12)
SEC = timedelta(seconds=1)
SYM = "SOLUSDT"          # 가격 100, step 0.001
SYM2 = "XRPUSDT"         # 가격 2
PERMANENT = ApiResult(False, kind="permanent", error="HTTP 400 code -1013: Filter failure")


class Crash(BaseException):
    """프로세스가 죽는 것을 흉내 낸다(엔진의 except Exception에 잡히지 않는다)."""


def codes(report) -> set[str]:
    return {h["code"] for h in report.halts}


def calls(ex, name: str) -> list[dict]:
    return [p for n, p in ex.calls if n == name]


def names(ex) -> list[str]:
    return [n for n, _ in ex.calls]


def events(report, kind: str, symbol: str | None = None) -> list[dict]:
    return [e for e in report.events if e["type"] == kind and (symbol is None or e["symbol"] == symbol)]


def at(i: int):
    return T0 + 30 * i * SEC


class EngineEntryTests(unittest.TestCase):
    def build(self, signals=(SYM,), balances=None, starting_capital=1000.0, rule_overrides=None,
              open_ratios=None):
        eng, ex, feed, store = make_engine(test=self, balances=balances, starting_capital=starting_capital,
                                           rule_overrides=rule_overrides)
        for symbol in signals:
            set_signal(ex, feed, symbol, open_ratio=(open_ratios or {}).get(symbol, 0.95))
        eng.startup(T0)
        return eng, ex, feed, store

    def loops(self, eng, start: int, count: int):
        return [eng.run_once(at(i)) for i in range(start, start + count)]

    @staticmethod
    def trades(store) -> list[dict]:
        return store.load("trades", [])

    # ---- 신호 확인 ------------------------------------------------------------------
    def test_signal_needs_three_consecutive_loops(self):
        eng, ex, feed, store = self.build()
        eng.run_once(at(0))
        eng.run_once(at(1))
        self.assertEqual(calls(ex, "market_buy"), [])
        r = eng.run_once(at(2))
        buys = calls(ex, "market_buy")
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["symbol"], SYM)
        self.assertIn(SYM, store.load("positions", {}))
        self.assertEqual(len(events(r, "BUY", SYM)), 1)

    def test_falling_market_blocks_entries_unless_demo_bypass(self):
        for bypass, expected in ((False, 0), (True, 1)):
            with self.subTest(bypass=bypass):
                eng, ex, feed, store = make_engine(test=self)
                set_signal(ex, feed, SYM)
                ex.set_candles("BTCUSDT", trending_candles(70, 120000.0, -400.0))   # 하락하는 60일선 아래
                eng.settings = dataclasses.replace(eng.settings, demo_ignore_market_filter=bypass)
                eng.startup(T0)
                self.loops(eng, 0, 3)
                self.assertEqual(len(calls(ex, "market_buy")), expected)

    def test_demo_bypass_announced_at_startup(self):
        eng, ex, feed, store = make_engine(test=self)
        eng.settings = dataclasses.replace(eng.settings, demo_ignore_market_filter=True)
        eng.startup(T0)
        self.assertTrue(any("시장 필터" in e["reason"] for e in eng.events if e["type"] == "SYSTEM"))

    def test_confirmations_reset_when_loop_not_evaluated(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 2)                              # 2회 확인
        feed.set_health(False)
        r = eng.run_once(at(2))                            # 중단: 평가하지 않음 → 처음부터
        self.assertIn(FEED_STALE, codes(r))
        feed.set_health(True)
        self.loops(eng, 3, 2)                              # 평가 1, 2회
        self.assertEqual(calls(ex, "market_buy"), [])
        eng.run_once(at(5))                                # 연속 평가 3회째
        self.assertEqual(len(calls(ex, "market_buy")), 1)

    def test_no_second_attempt_without_reconfirmation(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 2)
        ex.set_fill_ratio(0.0)                             # 체결 0: 거절
        eng.run_once(at(2))
        self.assertEqual(len(calls(ex, "market_buy")), 1)
        self.loops(eng, 3, 2)                              # 다시 1, 2회 확인: 주문 없음
        self.assertEqual(len(calls(ex, "market_buy")), 1)
        eng.run_once(at(5))                                # 다시 3회 확인: 두 번째 시도
        self.assertEqual(len(calls(ex, "market_buy")), 2)
        self.assertIn(SYM, store.load("positions", {}))

    def test_report_counts_raw_and_confirmed_signals(self):
        eng, ex, feed, store = self.build(signals=(SYM, SYM2))
        r1 = eng.run_once(at(0))
        self.assertEqual((r1.raw_signals, r1.confirmed_signals), (2, 0))
        r2 = eng.run_once(at(1))
        self.assertEqual((r2.raw_signals, r2.confirmed_signals), (2, 0))
        progress = eng.snapshot(at(1))["signal_progress"]
        self.assertEqual(sorted((p["symbol"], p["strategy"], p["count"]) for p in progress),
                         [(SYM, "vbo", 2), (SYM2, "vbo", 2)])
        r3 = eng.run_once(at(2))
        self.assertEqual((r3.raw_signals, r3.confirmed_signals), (2, 2))

    # ---- 체결 반영 ------------------------------------------------------------------
    def test_buy_records_net_qty_after_base_fee(self):
        eng, ex, feed, store = self.build(balances={"USDT": 100.0})   # 현금 한도로 정확히 1.0
        self.loops(eng, 0, 3)
        self.assertAlmostEqual(calls(ex, "market_buy")[0]["qty"], 1.0)
        pos = store.load("positions", {})[SYM]
        self.assertAlmostEqual(pos["qty"], 0.999, places=12)
        trade = self.trades(store)[0]
        self.assertEqual(trade["type"], "BUY")
        self.assertAlmostEqual(trade["qty"], 0.999, places=12)
        self.assertAlmostEqual(trade["fees"][SYM[:-4]], 0.001)
        self.assertEqual(pos["cost"], trade["quote_qty"])
        self.assertAlmostEqual(pos["cost"], 100.0)

    def test_position_fields_initialized(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 3)
        pos = store.load("positions", {})[SYM]
        trade = self.trades(store)[0]
        avg = trade["quote_qty"] / calls(ex, "market_buy")[0]["qty"]
        self.assertAlmostEqual(pos["buy_price"], avg)
        self.assertAlmostEqual(pos["buy_price"], 100.0)
        self.assertEqual(pos["stop_loss_pct"], 0.04)
        self.assertEqual(pos["stop_price"], pos["buy_price"] * (1 - pos["stop_loss_pct"]))
        self.assertEqual(pos["highest_price"], pos["buy_price"])
        self.assertEqual(pos["trailing_gap_pct"], 0.04)
        self.assertFalse(pos["trailing_active"])
        self.assertEqual(pos["entry_time"], at(2).isoformat())
        self.assertEqual(pos["strategy"], "vbo")
        self.assertEqual(pos["symbol"], SYM)
        self.assertEqual((trade["symbol"], trade["strategy"], trade["reason"]), (SYM, "vbo", "vbo"))
        self.assertTrue(trade["client_id"].startswith("cs-d-en-"))
        self.assertEqual(trade["time"], at(2).isoformat())
        self.assertAlmostEqual(trade["price"], 100.0)
        self.assertAlmostEqual(trade["slippage_pct"], 0.0)
        self.assertEqual(store.load("intents", {}), {})
        self.assertEqual(store.load("risk_state", {})["realized_pnl"], 0.0)

    def test_cash_after_buy_matches_exchange_usdt_change(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 3)
        pos = store.load("positions", {})[SYM]
        cash, _ = account_value(store.load("risk_state", {}), store.load("positions", {}), {SYM: 100.0})
        self.assertAlmostEqual(cash, 1000.0 - pos["cost"])
        usdt = ex.balances().value["USDT"][0]
        self.assertAlmostEqual(10000.0 - usdt, pos["cost"])

    def test_protective_stop_placed_right_after_buy(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 2)
        ex.calls.clear()
        eng.run_once(at(2))
        seq = names(ex)
        self.assertLess(seq.index("market_buy"), seq.index("place_stop"))
        stop = calls(ex, "place_stop")[0]
        pos = store.load("positions", {})[SYM]
        self.assertAlmostEqual(stop["qty"], 1.748)          # 받은 1.74825를 step으로 내림
        self.assertAlmostEqual(stop["stop_price"], 96.0)
        self.assertEqual(pos["protective"]["state"], "open")
        self.assertEqual(pos["protective"]["client_id"], stop["client_id"])

    def test_protective_place_failure_keeps_position_and_logs_risk(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 2)
        ex.fail_next("place_stop", PERMANENT)
        r = eng.run_once(at(2))
        pos = store.load("positions", {})[SYM]
        self.assertIsNone(pos["protective"])
        self.assertTrue(any("보호 주문 등록 실패" in e["reason"] for e in events(r, "RISK", SYM)))
        eng.run_once(at(3))                                 # 다음 루프의 청산 단계가 다시 건다
        self.assertEqual(len(calls(ex, "place_stop")), 2)
        self.assertEqual(store.load("positions", {})[SYM]["protective"]["state"], "open")

    def test_partial_buy_records_filled_part_only(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 2)
        ex.set_fill_ratio(0.5)
        eng.run_once(at(2))
        self.assertAlmostEqual(calls(ex, "market_buy")[0]["qty"], 1.75)
        pos = store.load("positions", {})[SYM]
        self.assertAlmostEqual(pos["qty"], 0.875 - 0.000875, places=12)
        self.assertAlmostEqual(pos["cost"], 87.5)
        trade = self.trades(store)[0]
        self.assertAlmostEqual(trade["quote_qty"], 87.5)
        self.assertEqual(store.load("intents", {}), {})

    def test_zero_fill_does_not_create_position(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 2)
        ex.set_fill_ratio(0.0)
        r = eng.run_once(at(2))
        self.assertEqual(store.load("positions", {}), {})
        self.assertEqual(self.trades(store), [])
        self.assertEqual(store.load("intents", {}), {})
        self.assertTrue(any("매수 거절" in e["reason"] for e in events(r, "RISK", SYM)))
        self.assertNotIn(ORDER_UNCONFIRMED, codes(r))

    def test_unconfirmed_buy_sets_halt_and_stops_further_entries(self):
        eng, ex, feed, store = self.build(signals=(SYM, SYM2), open_ratios={SYM2: 0.93})
        self.loops(eng, 0, 2)
        ex.hide_order_for(config.ORDER_CONFIRM_TIMEOUT_SECONDS)
        r = eng.run_once(at(2))
        buys = calls(ex, "market_buy")
        self.assertEqual([b["symbol"] for b in buys], [SYM])     # 두 번째 신호는 시도하지 않는다
        self.assertIn(ORDER_UNCONFIRMED, codes(r))
        self.assertEqual(store.load("positions", {}), {})
        self.assertEqual(len(store.load("intents", {})), 1)
        r2 = eng.run_once(at(3))                            # 다음 루프 1단계가 체결을 반영한다
        self.assertIn(SYM, store.load("positions", {}))
        self.assertEqual([t["symbol"] for t in self.trades(store) if t["type"] == "BUY"][0], SYM)
        self.assertNotIn(ORDER_UNCONFIRMED, codes(r2))
        # SOL은 다시 확인해야 하고, 계속 확인된 XRP는 이 루프에 산다(3단계 루프도 평가된 루프였다)
        self.assertEqual([b["symbol"] for b in calls(ex, "market_buy")], [SYM, SYM2])

    def test_buy_journal_replays_idempotently_after_crash(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 2)
        real_save = store.save

        def save(name, data):
            if name == "positions" and any(t["type"] == "BUY" for t in store.load("trades", [])):
                raise Crash()                               # 체결 기록 저장 뒤, 포지션 저장 전
            real_save(name, data)
        store.save = save
        with self.assertRaises(Crash):
            eng.run_once(at(2))
        store.save = real_save
        self.assertEqual(len(self.trades(store)), 1)
        self.assertEqual(store.load("positions", {}), {})
        self.assertIn("journal", next(iter(store.load("intents", {}).values())))
        eng2 = restart_engine(eng, at(3))
        eng2.run_once(at(3))
        trades = self.trades(store)
        self.assertEqual([t["type"] for t in trades], ["BUY"])
        positions = store.load("positions", {})
        self.assertEqual(list(positions), [SYM])
        self.assertEqual(store.load("intents", {}), {})
        self.assertEqual(len(calls(ex, "market_buy")), 1)
        cash, _ = account_value(store.load("risk_state", {}), positions, {SYM: 100.0})
        self.assertAlmostEqual(cash, 1000.0 - positions[SYM]["cost"])
        self.assertAlmostEqual(10000.0 - ex.balances().value["USDT"][0], positions[SYM]["cost"])

    def test_buy_intent_dropped_after_grace_records_risk(self):
        eng, ex, feed, store = make_engine(test=self)
        store.save("intents", add_intent({}, "cs-d-en-000000000000", SYM, "BUY", 1.0, "entry", "vbo", T0))
        eng.startup(T0)
        r = eng.run_once(T0)
        self.assertIn(ORDER_UNCONFIRMED, codes(r))
        r2 = eng.run_once(T0 + config.INTENT_NOT_FOUND_GRACE_SECONDS * SEC)
        self.assertEqual(store.load("intents", {}), {})
        self.assertEqual([e["reason"] for e in events(r2, "RISK", SYM)],
                         ["매수 주문 미확인 폐기: 거래소 잔고 확인 필요"])
        self.assertEqual(calls(ex, "market_buy"), [])

    # ---- 시세와 수량 ------------------------------------------------------------------
    def test_entry_uses_live_quote_price_for_sizing(self):
        eng, ex, feed, store = self.build()
        from coin_sniper import entries
        real = entries.evaluate_signals

        def doubled(*args, **kwargs):
            out = real(*args, **kwargs)
            for sig in out:
                sig["entry_price"] *= 2                     # 캔들 종가가 아니라 실시간 시세로 정해야 한다
            return out
        with mock.patch.object(entries, "evaluate_signals", doubled):
            self.loops(eng, 0, 3)
        buy = calls(ex, "market_buy")[0]
        self.assertAlmostEqual(buy["qty"], 1.75)            # 1000×0.7% ÷ (100×0.04)
        intent_ref = self.trades(store)[0]
        self.assertAlmostEqual(intent_ref["slippage_pct"], 0.0)

    def test_stale_quote_at_evaluation_gives_no_signal(self):
        eng, ex, feed, store = make_engine(test=self)
        set_signal(ex, feed, SYM, age=100)                  # 일봉(REST)은 신호, 실시간 시세는 오래됨
        eng.startup(T0)
        reports = self.loops(eng, 0, 3)
        self.assertTrue(all(r.raw_signals == 0 for r in reports))   # 실시간 봉이 아니면 평가하지 않는다
        self.assertEqual(calls(ex, "market_buy"), [])
        self.assertEqual(events(reports[2], "SKIP", SYM), [])

    def test_quote_stale_between_evaluation_and_sizing_skips(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 2)
        from coin_sniper import entries
        real = entries.confirm_persistent_signals

        def then_stale(*args):
            out = real(*args)
            set_signal(ex, feed, SYM, age=100)              # 평가 뒤, 수량 산정 전에 시세가 오래됨
            return out
        with mock.patch.object(entries, "confirm_persistent_signals", then_stale):
            r = eng.run_once(at(2))
        self.assertEqual(r.confirmed_signals, 1)
        self.assertEqual(calls(ex, "market_buy"), [])
        self.assertEqual([e["reason"] for e in events(r, "SKIP", SYM)], ["실시간 시세 없음"])

    def test_stale_quote_loops_do_not_count_toward_confirmation(self):
        eng, ex, feed, store = make_engine(test=self)
        set_signal(ex, feed, SYM, age=100)
        eng.startup(T0)
        self.loops(eng, 0, 2)                               # 오래된 시세 2회: 확인 아님
        set_signal(ex, feed, SYM)
        self.loops(eng, 2, 2)                               # 실시간 1, 2회
        self.assertEqual(calls(ex, "market_buy"), [])
        eng.run_once(at(4))                                 # 실시간 3회 연속
        self.assertEqual(len(calls(ex, "market_buy")), 1)

    def test_market_proxy_quote_from_other_day_resets_confirmations(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 2)
        feed.set_quote("BTCUSDT", 90000.0, day="2026-11-16")   # 신선하지만 오늘 봉이 아니다
        r = eng.run_once(at(2))
        self.assertEqual(r.raw_signals, 0)
        self.assertEqual(eng.snapshot(at(2))["signal_progress"], [])
        feed.set_quote("BTCUSDT", 90000.0)
        self.loops(eng, 3, 2)
        self.assertEqual(calls(ex, "market_buy"), [])
        eng.run_once(at(5))
        self.assertEqual(len(calls(ex, "market_buy")), 1)

    def test_failed_evaluation_resets_confirmations(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 2)
        from coin_sniper import entries
        with mock.patch.object(entries, "evaluate_signals", side_effect=RuntimeError("boom")):
            r = eng.run_once(at(2))
        self.assertFalse(r.ok)
        self.loops(eng, 3, 2)                               # 실패 뒤 1, 2회
        self.assertEqual(calls(ex, "market_buy"), [])
        eng.run_once(at(5))                                 # 실패 뒤 연속 3회
        self.assertEqual(len(calls(ex, "market_buy")), 1)

    @staticmethod
    def mark_suspect(eng, symbol: str) -> None:
        """매수 의도가 유예 뒤 폐기된 종목으로 표시한다(BUY_DROPPED가 남기는 것과 같은 기록)."""
        eng.risk_state.setdefault("suspect_symbols", []).append({"symbol": symbol, "since": T0.isoformat()})

    def test_symbol_with_unmanaged_exchange_balance_skipped(self):
        eng, ex, feed, store = make_engine(test=self, balances={"USDT": 10000.0, "SOL": 1.0})
        set_signal(ex, feed, SYM)
        set_signal(ex, feed, SYM2, open_ratio=0.93)
        eng.startup(T0)
        self.mark_suspect(eng, SYM)
        reports = self.loops(eng, 0, 3)
        self.assertEqual([b["symbol"] for b in calls(ex, "market_buy")], [SYM2])
        self.assertEqual([e["reason"] for e in events(reports[2], "SKIP", SYM)], ["거래소에 미관리 잔고 있음"])
        self.assertNotIn(SYM, store.load("positions", {}))

    def test_dust_left_after_exit_does_not_block_reentry(self):
        eng, ex, feed, store = self.build()
        entry = (T0 - timedelta(hours=config.MAX_HOLD_HOURS)).isoformat()
        add_position(eng, ex, SYM, 1.0005, 100.0, entry_time=entry)   # 타임스탑: 1.0 매도, 0.0005 잔량
        reports = self.loops(eng, 0, 3)
        self.assertEqual(len(calls(ex, "market_sell")), 1)
        self.assertIn("SOL", eng.unmanaged_assets)          # 팔 수 없는 잔량이 거래소에 남았다
        self.assertEqual(events(reports[2], "SKIP", SYM), [])
        self.assertEqual([b["symbol"] for b in calls(ex, "market_buy")], [SYM])
        self.assertIn(SYM, store.load("positions", {}))

    def unmanaged_case(self, sol_balance: float, suspect: bool = True):
        eng, ex, feed, store = make_engine(test=self, balances={"USDT": 10000.0, "SOL": sol_balance})
        set_signal(ex, feed, SYM)
        eng.startup(T0)
        if suspect:
            self.mark_suspect(eng, SYM)
        reports = self.loops(eng, 0, 3)
        return ex, reports

    def test_unmanaged_balance_exactly_min_notional_is_skipped(self):
        ex, reports = self.unmanaged_case(0.05)              # 0.05 × 100 = 5 = 최소 주문 금액
        self.assertEqual(calls(ex, "market_buy"), [])
        self.assertEqual([e["reason"] for e in events(reports[2], "SKIP", SYM)], ["거래소에 미관리 잔고 있음"])

    def test_unmanaged_balance_below_min_notional_is_bought(self):
        ex, reports = self.unmanaged_case(0.0499)            # 내림 0.049 × 100 = 4.9 < 5
        self.assertEqual([b["symbol"] for b in calls(ex, "market_buy")], [SYM])
        self.assertEqual(events(reports[2], "SKIP", SYM), [])

    def test_prefunded_balance_of_unsuspected_symbol_does_not_block_buy(self):
        ex, reports = self.unmanaged_case(1.0, suspect=False)   # 데모 계정에 미리 들어 있던 코인
        self.assertEqual([b["symbol"] for b in calls(ex, "market_buy")], [SYM])
        self.assertEqual(events(reports[2], "SKIP", SYM), [])

    def test_prefunded_btc_does_not_block_btc_signal(self):
        eng, ex, feed, store = make_engine(test=self, balances={"USDT": 10000.0, "BTC": 1.0})
        set_signal(ex, feed, "BTCUSDT")
        eng.startup(T0)
        self.loops(eng, 0, 3)
        self.assertEqual([b["symbol"] for b in calls(ex, "market_buy")], ["BTCUSDT"])

    def test_buy_dropped_marks_symbol_suspect_and_later_signal_is_skipped(self):
        eng, ex, feed, store = make_engine(test=self, balances={"USDT": 10000.0, "SOL": 1.0})
        set_signal(ex, feed, SYM)
        store.save("intents", add_intent({}, "cs-d-en-000000000000", SYM, "BUY", 1.0, "entry", "vbo", T0))
        eng.startup(T0)
        eng.run_once(T0)                                    # 결과 모름: 유예 중
        drop_at = T0 + config.INTENT_NOT_FOUND_GRACE_SECONDS * SEC
        eng.run_once(drop_at)                               # 유예 지나 폐기 → 의심 종목
        suspects = store.load("risk_state", {})["suspect_symbols"]
        self.assertEqual(suspects, [{"symbol": SYM, "since": drop_at.isoformat()}])
        reports = [eng.run_once(drop_at + 30 * i * SEC) for i in range(1, 4)]
        self.assertEqual(calls(ex, "market_buy"), [])       # 늦게 체결됐을 수 있는 SOL을 두 번 사지 않는다
        self.assertIn("거래소에 미관리 잔고 있음", [e["reason"] for r in reports for e in events(r, "SKIP", SYM)])

    def test_no_entry_during_blackout(self):
        eng, ex, feed, store = self.build()
        reports = [eng.run_once(utc(2026, 11, 17, 0, 10, 30 * i)) for i in range(2)]
        reports += [eng.run_once(utc(2026, 11, 17, 0, 11 + i // 2, 30 * (i % 2))) for i in range(4)]
        self.assertEqual(calls(ex, "market_buy"), [])
        self.assertTrue(all(r.raw_signals == 0 and not r.entry_allowed for r in reports))

    def test_no_entry_when_any_halt_active(self):
        eng, ex, feed, store = self.build()
        feed.set_health(False)
        reports = self.loops(eng, 0, 4)
        self.assertTrue(all(FEED_STALE in codes(r) for r in reports))
        self.assertEqual(calls(ex, "market_buy"), [])
        self.assertTrue(all(r.raw_signals == 0 for r in reports))

    def test_no_entry_when_btc_below_falling_ma(self):
        eng, ex, feed, store = make_engine(test=self)
        set_signal(ex, feed, SYM)
        ex.set_candles("BTCUSDT", trending_candles(70, 100000.0, -150.0))
        eng.startup(T0)
        reports = self.loops(eng, 0, 4)
        self.assertTrue(all(r.entry_allowed for r in reports))
        self.assertTrue(all(r.raw_signals == 0 for r in reports))
        self.assertEqual(calls(ex, "market_buy"), [])

    def test_max_positions_respected(self):
        eng, ex, feed, store = self.build(signals=(SYM, SYM2), open_ratios={SYM2: 0.93})
        add_position(eng, ex, "ETHUSDT", 0.01, 3000.0)
        add_position(eng, ex, "LINKUSDT", 1.0, 15.0)
        reports = self.loops(eng, 0, 3)
        self.assertEqual([b["symbol"] for b in calls(ex, "market_buy")], [SYM])
        self.assertEqual(len(store.load("positions", {})), config.MAX_POSITIONS)
        self.assertEqual([e["reason"] for e in events(reports[2], "SKIP", SYM2)], ["최대 보유 종목 수 도달"])

    def test_already_held_symbol_skipped(self):
        eng, ex, feed, store = self.build()
        add_position(eng, ex, SYM, 1.0, 100.0)
        reports = self.loops(eng, 0, 3)
        self.assertEqual(calls(ex, "market_buy"), [])
        self.assertEqual([e["reason"] for e in events(reports[2], "SKIP", SYM)], ["이미 보유 중"])

    def test_symbol_with_open_intent_skipped(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 2)
        add_intent(eng.intents, "cs-d-ex-000000000000", SYM, "SELL", 1.0, "exit", "x", at(2))
        with mock.patch.object(eng.executor, "resolve_intents"):   # 1단계가 해소하지 않은 의도
            r = eng.run_once(at(2))
        self.assertEqual(r.confirmed_signals, 1)
        self.assertEqual(calls(ex, "market_buy"), [])
        self.assertEqual([e["reason"] for e in events(r, "SKIP", SYM)], ["미확정 주문 있음"])

    def test_symbol_without_trading_rule_skipped(self):
        eng, ex, feed, store = self.build()
        self.loops(eng, 0, 2)
        eng.rules.pop(SYM)
        r = eng.run_once(at(2))
        self.assertEqual(calls(ex, "market_buy"), [])
        self.assertEqual([e["reason"] for e in events(r, "SKIP", SYM)], ["종목 규칙 없음"])

    def test_third_position_shrinks_to_remaining_open_risk(self):
        eng, ex, feed, store = self.build()
        add_position(eng, ex, "ETHUSDT", 0.07, 3000.0, stop_price=2900.0)   # 위험 7 = 0.7%
        add_position(eng, ex, SYM2, 70.0, 2.0, stop_price=1.9)              # 위험 7 = 0.7%
        self.loops(eng, 0, 3)
        qty = calls(ex, "market_buy")[0]["qty"]
        planned_loss = qty * 100.0 * 0.04
        self.assertLessEqual(planned_loss, 1000.0 * 0.006 + 1e-9)
        self.assertAlmostEqual(planned_loss, 1000.0 * 0.006, delta=0.01)

    def test_below_min_notional_skips_with_event(self):
        big = SymbolRules(symbol=SYM, base="SOL", quote="USDT", trading=True, step_size=0.001,
                          tick_size=0.0001, min_notional=1000.0)
        eng, ex, feed, store = self.build(rule_overrides={SYM: big})
        reports = self.loops(eng, 0, 3)
        self.assertEqual(calls(ex, "market_buy"), [])
        self.assertEqual([e["reason"] for e in events(reports[2], "SKIP", SYM)],
                         ["투자·위험 한도 또는 최소 주문 금액 미달"])

    def test_cash_limited_by_actual_free_usdt(self):
        eng, ex, feed, store = self.build(balances={"USDT": 100.0})
        reports = self.loops(eng, 0, 3)
        buy = calls(ex, "market_buy")[0]
        self.assertLessEqual(buy["qty"] * 100.0, 100.0)
        self.assertIn("실제 USDT가 봇 현금보다 적음", [e["reason"] for e in events(reports[2], "RISK")])

    def test_two_signals_with_cash_for_only_one(self):
        eng, ex, feed, store = self.build(signals=(SYM, SYM2), balances={"USDT": 178.0},
                                          open_ratios={SYM2: 0.93})
        reports = self.loops(eng, 0, 3)
        self.assertEqual([b["symbol"] for b in calls(ex, "market_buy")], [SYM])   # 남은 3 USDT < 최소 5
        self.assertEqual(list(store.load("positions", {})), [SYM])
        self.assertEqual([e["reason"] for e in events(reports[2], "SKIP", SYM2)],
                         ["투자·위험 한도 또는 최소 주문 금액 미달"])

    def test_two_signals_both_bought_in_one_loop(self):
        eng, ex, feed, store = self.build(signals=(SYM, SYM2), open_ratios={SYM2: 0.93})
        self.loops(eng, 0, 3)
        self.assertEqual([b["symbol"] for b in calls(ex, "market_buy")], [SYM, SYM2])
        self.assertAlmostEqual(calls(ex, "market_buy")[1]["qty"], 87.5)   # 1000×0.7% ÷ (2×0.04)
        self.assertEqual(sorted(store.load("positions", {})), [SYM, SYM2])

    def test_drawdown_multiplier_halves_size(self):
        eng, ex, feed, store = self.build()
        eng.risk_state["peak_equity"] = 1100.0              # 낙폭 9.1%: 배율 0.5
        reports = self.loops(eng, 0, 3)
        self.assertEqual(eng.size_multiplier, 0.5)
        self.assertAlmostEqual(calls(ex, "market_buy")[0]["qty"], 0.875)


if __name__ == "__main__":
    unittest.main()
