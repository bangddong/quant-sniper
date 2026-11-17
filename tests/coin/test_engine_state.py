import unittest
from unittest import mock
from datetime import date, timedelta

from coin_sniper import config
from coin_sniper.engine import Engine, LoopReport
from coin_sniper.halts import (
    ACCOUNT_FETCH, DAILY_LOSS, DRAWDOWN, FATAL, FEED_STALE, LOOP_ERROR, RATE_LIMIT, RECON_MISMATCH,
    UNIVERSE,
)
from coin_sniper.metrics import Metrics
from coin_sniper.models import ApiResult

from helpers import DEFAULT_PRICES, make_engine, position, trending_candles, utc

T0 = utc(2026, 11, 17, 12)
SEC = timedelta(seconds=1)


def codes(report: LoopReport) -> set[str]:
    return {h["code"] for h in report.halts}


def calls_named(ex, name: str) -> int:
    return sum(1 for n, _ in ex.calls if n == name)


class EngineStateTests(unittest.TestCase):
    def build(self, positions=None, risk_state=None, **kw):
        eng, ex, feed, store = make_engine(test=self, **kw)
        if positions is not None:
            store.save("positions", positions)
        if risk_state is not None:
            store.save("risk_state", risk_state)
        eng.startup(T0)
        return eng, ex, feed, store

    def test_first_run_initializes_virtual_account(self):
        eng, ex, feed, store = self.build()
        r = eng.run_once(T0)
        self.assertTrue(r.ok)
        rs = store.load("risk_state", {})
        self.assertEqual(rs["starting_capital"], 1000)
        self.assertEqual(rs["realized_pnl"], 0)
        self.assertEqual(rs["day"], "2026-11-17")
        self.assertEqual(rs["day_open_equity"], 1000)
        self.assertEqual(rs["segment_start"], T0.isoformat())
        self.assertEqual(rs["last_loop_at"], T0.isoformat())
        self.assertEqual(r.equity, 1000)
        self.assertEqual(r.cash, 1000)
        self.assertEqual(r.halts, [])
        self.assertTrue(r.entry_allowed)
        self.assertEqual(feed.started, 1)
        self.assertIn("BTCUSDT", feed.symbols)

    def test_account_fetch_failure_sets_halt_and_recovery_clears_it(self):
        eng, ex, feed, store = self.build()
        ex.fail_next("balances", ApiResult(False, kind="transient", error="502"))
        r1 = eng.run_once(T0)
        self.assertIn(ACCOUNT_FETCH, codes(r1))
        self.assertFalse(r1.entry_allowed)
        self.assertEqual([e["type"] for e in r1.events], ["RISK"])
        r2 = eng.run_once(T0 + 30 * SEC)
        self.assertNotIn(ACCOUNT_FETCH, codes(r2))
        self.assertTrue(r2.entry_allowed)
        self.assertEqual([e["type"] for e in r2.events], ["SYSTEM"])

    def test_universe_selected_once_per_utc_day_from_previous_day_volume(self):
        eng, ex, feed, store = self.build()
        low = trending_candles(70, 100, 1, quote_vol=5e7)
        low[-2]["quote_vol"] = config.MIN_PREV_DAY_QUOTE_VOLUME - 1   # 전일은 하한 미달
        low[-1]["quote_vol"] = 9e9                                     # 진행 중 봉은 거대
        ex.set_candles("LINKUSDT", low)
        big = trending_candles(70, 100, 1)
        big[-2]["quote_vol"] = 9e8
        ex.set_candles("DOGEUSDT", big)
        for i in range(3):
            eng.run_once(T0 + 30 * i * SEC)
        self.assertEqual(calls_named(ex, "symbol_rules"), 1)
        uni = store.load("universe", {})
        self.assertEqual(uni["date"], "2026-11-17")
        self.assertNotIn("LINKUSDT", uni["symbols"])
        self.assertEqual(uni["symbols"][0], "DOGEUSDT")
        self.assertEqual(len(uni["symbols"]), 6)
        eng.run_once(utc(2026, 11, 18, 0, 20))
        self.assertEqual(calls_named(ex, "symbol_rules"), 2)
        self.assertEqual(store.load("universe", {})["date"], "2026-11-18")

    def test_universe_failure_blocks_entries_without_fallback(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)                                   # 오늘 목록 성공
        ex.fail_next("symbol_rules", ApiResult(False, kind="transient", error="HTTP 503"))
        t1 = utc(2026, 11, 18, 1)
        r = eng.run_once(t1)
        self.assertIn(UNIVERSE, codes(r))
        self.assertFalse(r.entry_allowed)
        self.assertEqual(store.load("universe", {}), {"date": "2026-11-18", "symbols": []})
        eng.run_once(t1 + 30 * SEC)                         # 10분 전에는 재시도하지 않는다
        self.assertEqual(calls_named(ex, "symbol_rules"), 2)
        r3 = eng.run_once(t1 + config.UNIVERSE_RETRY_SECONDS * SEC)
        self.assertEqual(calls_named(ex, "symbol_rules"), 3)
        self.assertNotIn(UNIVERSE, codes(r3))
        self.assertEqual(len(store.load("universe", {})["symbols"]), 7)

    def test_universe_below_min_count_blocks_entries(self):
        eng, ex, feed, store = self.build()
        for sym in ("ADAUSDT", "DOGEUSDT", "LINKUSDT"):
            ex.set_candles(sym, trending_candles(70, 1, 0.01, quote_vol=1e6))
        r = eng.run_once(T0)
        self.assertIn(UNIVERSE, codes(r))
        self.assertFalse(r.entry_allowed)
        self.assertEqual(store.load("universe", {})["symbols"], [])
        self.assertEqual(eng.snapshot(T0)["universe"], [])

    def test_day_rollover_resets_daily_baseline_and_appends_equity(self):
        eng, ex, feed, store = self.build(positions={"SOLUSDT": position("SOLUSDT", 1.0, 100)},
                                          balances={"USDT": 10000.0, "SOL": 1.0})
        feed.set_quote("SOLUSDT", 100)
        eng.run_once(T0)
        feed.set_quote("SOLUSDT", 60)                       # 960 → 일일 손실
        r = eng.run_once(T0 + 30 * SEC)
        self.assertIn(DAILY_LOSS, codes(r))
        day2 = utc(2026, 11, 18, 0, 20)
        btc = trending_candles(70, 50000, 100, today=date(2026, 11, 18))
        ex.set_candles("BTCUSDT", btc)
        for sym in ("BTCUSDT", "SOLUSDT"):
            feed.set_quote(sym, 90000 if sym == "BTCUSDT" else 70, day="2026-11-18")
        r2 = eng.run_once(day2)
        self.assertNotIn(DAILY_LOSS, codes(r2))
        rs = store.load("risk_state", {})
        self.assertEqual(rs["day"], "2026-11-18")
        self.assertEqual(rs["day_open_equity"], 970)
        self.assertEqual(store.load("equity_history", []),
                         [{"day": "2026-11-17", "equity": 960, "btc": btc[-2]["close"]}])
        self.assertNotIn("pending_equity_row", rs)

    def test_restart_after_days_offline_rebuilds_day_state(self):
        old = utc(2026, 11, 13, 12)
        eng, ex, feed, store = make_engine(test=self)
        store.save("risk_state", {"starting_capital": 1000.0, "realized_pnl": 0.0, "peak_equity": 1000.0,
                                  "day": "2026-11-13", "day_open_equity": 1000.0,
                                  "segment_start": old.isoformat(), "last_loop_at": old.isoformat(),
                                  "last_equity": 1000.0})
        store.save("universe", {"date": "2026-11-13", "symbols": ["BTCUSDT"]})
        eng.startup(T0)
        r = eng.run_once(T0)
        self.assertTrue(r.ok)
        rs = store.load("risk_state", {})
        self.assertEqual(rs["day"], "2026-11-17")
        self.assertEqual(rs["day_open_equity"], 1000)
        self.assertEqual(store.load("universe", {})["date"], "2026-11-17")
        self.assertEqual(len(store.load("universe", {})["symbols"]), 7)
        offline = eng.metrics.summary(T0, None)["offline_seconds"]
        self.assertEqual(offline, 4 * 86400)
        self.assertEqual(eng.metrics.summary(T0, None)["restarts"], 1)
        btc_on_day = next(c["close"] for c in trending_candles(70, 72000, 270) if c["date"] == "2026-11-13")
        self.assertEqual(store.load("equity_history", []),
                         [{"day": "2026-11-13", "equity": 1000.0, "btc": btc_on_day}])   # 날짜로 고른 종가
        self.assertNotIn("pending_equity_row", rs)

    def test_ledger_qty_missing_on_exchange_sets_recon_halt(self):
        eng, ex, feed, store = self.build(positions={"SOLUSDT": position("SOLUSDT", 1.0, 100)},
                                          balances={"USDT": 10000.0, "SOL": 0.5})
        r1 = eng.run_once(T0)
        self.assertIn(RECON_MISMATCH, codes(r1))
        self.assertIn("SOLUSDT", r1.halts[0]["detail"])
        self.assertFalse(r1.entry_allowed)
        snap = eng.snapshot(T0 + (config.RECON_ESCALATE_SECONDS + 1) * SEC)
        self.assertTrue(snap["halts"][0]["needs_manual"])
        self.assertNotIn("needs_manual", eng.snapshot(T0 + 60 * SEC)["halts"][0])
        self.assertTrue(ex.market_buy("SOLUSDT", 0.501, "t-restore").ok)   # 0.5 + 0.501×0.999 ≥ 1.0
        r2 = eng.run_once(T0 + 30 * SEC)
        self.assertNotIn(RECON_MISMATCH, codes(r2))

    def test_recon_tolerates_less_than_one_step(self):
        eng, ex, feed, store = self.build(positions={"SOLUSDT": position("SOLUSDT", 1.0, 100)},
                                          balances={"USDT": 10000.0, "SOL": 0.9995})
        self.assertNotIn(RECON_MISMATCH, codes(eng.run_once(T0)))
        eng2, *_ = self.build(positions={"SOLUSDT": position("SOLUSDT", 1.0, 100)},
                              balances={"USDT": 10000.0, "SOL": 0.998})
        self.assertIn(RECON_MISMATCH, codes(eng2.run_once(T0)))

    def test_unmanaged_assets_are_ignored(self):
        eng, ex, feed, store = self.build(balances={"USDT": 10000.0, "ETH": 5.0, "BNB": 0.0})
        r = eng.run_once(T0)
        self.assertEqual(r.halts, [])
        self.assertEqual(r.equity, 1000)
        snap = eng.snapshot(T0)
        self.assertEqual(snap["unmanaged_assets"], ["ETH"])
        self.assertEqual(snap["equity"], 1000)
        self.assertTrue(snap["entry_allowed"])

    def test_daily_loss_halts_until_next_utc_day(self):
        eng, ex, feed, store = self.build(positions={"SOLUSDT": position("SOLUSDT", 1.0, 100)},
                                          balances={"USDT": 10000.0, "SOL": 1.0})
        eng.run_once(T0)
        feed.set_quote("SOLUSDT", 60)
        r = eng.run_once(T0 + 30 * SEC)
        self.assertEqual(r.equity, 960)
        self.assertIn(DAILY_LOSS, codes(r))
        feed.set_quote("SOLUSDT", 100)                       # 같은 날 회복해도 유지
        r2 = eng.run_once(T0 + 60 * SEC)
        self.assertEqual(r2.equity, 1000)
        self.assertIn(DAILY_LOSS, codes(r2))
        self.assertFalse(r2.entry_allowed)
        r3 = eng.run_once(utc(2026, 11, 18, 0, 30))
        self.assertNotIn(DAILY_LOSS, codes(r3))

    def test_drawdown_halt_clears_when_recovered(self):
        rs = {"starting_capital": 1000.0, "realized_pnl": 0.0, "peak_equity": 1200.0,
              "day": "2026-11-17", "day_open_equity": None, "segment_start": T0.isoformat()}
        eng, ex, feed, store = self.build(positions={"SOLUSDT": position("SOLUSDT", 1.0, 100)},
                                          risk_state=rs, balances={"USDT": 10000.0, "SOL": 1.0})
        r = eng.run_once(T0)
        self.assertIn(DRAWDOWN, codes(r))
        feed.set_quote("SOLUSDT", 300)
        r2 = eng.run_once(T0 + 30 * SEC)
        self.assertEqual(r2.equity, 1200)
        self.assertNotIn(DRAWDOWN, codes(r2))
        self.assertEqual(store.load("risk_state", {})["peak_equity"], 1200)

    def test_feed_unhealthy_sets_feed_stale(self):
        eng, ex, feed, store = self.build(positions={"SOLUSDT": position("SOLUSDT", 1.0, 100)},
                                          balances={"USDT": 10000.0, "SOL": 1.0})
        feed.set_health(False, "disconnected")
        r = eng.run_once(T0)
        self.assertIn(FEED_STALE, codes(r))
        self.assertFalse(r.entry_allowed)
        feed.set_health(True)
        feed.set_quote("SOLUSDT", 100, age=config.QUOTE_MAX_AGE_SECONDS + 1)   # 보유 종목 시세 오래됨
        r2 = eng.run_once(T0 + 30 * SEC)
        self.assertIn(FEED_STALE, codes(r2))
        self.assertIsNone(r2.equity)
        self.assertEqual(r2.cash, 900)
        feed.set_quote("SOLUSDT", 100)
        r3 = eng.run_once(T0 + 60 * SEC)
        self.assertNotIn(FEED_STALE, codes(r3))
        self.assertEqual(r3.equity, 1000)
        # 중단 시간: 직전 루프 이후 경과(2×주기 상한)를 사유별로 더한다
        self.assertEqual(eng.metrics.summary(T0, None)["halt_seconds"], {FEED_STALE: 60})

    def test_rate_limit_result_sets_halt_and_skips_calls_until_retry_after(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        ex.fail_next("balances", ApiResult(False, kind="transient", error="HTTP 429", retry_after=90))
        r1 = eng.run_once(T0 + 30 * SEC)
        self.assertIn(RATE_LIMIT, codes(r1))
        self.assertNotIn(ACCOUNT_FETCH, codes(r1))
        self.assertFalse(r1.entry_allowed)
        n = len(ex.calls)
        r2 = eng.run_once(T0 + 60 * SEC)
        self.assertEqual(len(ex.calls), n)
        self.assertIn(RATE_LIMIT, codes(r2))
        self.assertTrue(r2.ok)
        r3 = eng.run_once(T0 + 120 * SEC)
        self.assertGreater(len(ex.calls), n)
        self.assertNotIn(RATE_LIMIT, codes(r3))

    def test_fatal_result_sets_fatal_and_never_auto_clears(self):
        eng, ex, feed, store = self.build()
        ex.fail_next("balances", ApiResult(False, kind="fatal", error="HTTP 401 code -2015: Invalid API-key"))
        r1 = eng.run_once(T0)
        self.assertIn(FATAL, codes(r1))
        for i in range(1, 4):
            r = eng.run_once(T0 + 30 * i * SEC)
            self.assertIn(FATAL, codes(r))
            self.assertFalse(r.entry_allowed)
        self.assertIn(FATAL, codes(eng.run_once(utc(2026, 11, 18, 1))))

    def test_loop_exception_is_recorded_not_raised(self):
        eng, ex, feed, store = self.build()
        exits = self.record_exits(eng)

        def boom():
            raise ValueError("unexpected payload")
        ex.balances = boom
        r = eng.run_once(T0)
        self.assertFalse(r.ok)
        errors = [e for e in r.events if e["type"] == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("ValueError", errors[0]["reason"])
        self.assertIn("unexpected payload", errors[0]["reason"])
        self.assertIn(LOOP_ERROR, codes(r))
        self.assertFalse(r.entry_allowed)
        self.assertEqual(r.equity, 1000)                     # 다음 단계는 계속 실행된다
        self.assertEqual(len(exits), 1)
        self.assertEqual(eng.metrics.summary(T0, None)["loops_failed"], 1)
        self.assertIn("ERROR", [e["type"] for e in store.load("events", [])])
        self.assertEqual(store.load("metrics", {})["days"]["2026-11-17"]["loops_failed"], 1)

    # ---- fix round 1: 단계별 예외 격리, 청산 우선 ----------------------------------------
    def record_exits(self, eng, ex=None):
        seen = []

        def hook(now):
            seen.append(len(ex.calls) if ex is not None else now)
        eng._manage_exits = hook
        return seen

    def test_exits_run_when_reconcile_raises(self):
        eng, ex, feed, store = self.build()
        exits = self.record_exits(eng)

        def boom():
            raise KeyError("qty")
        eng._reconcile = boom
        r = eng.run_once(T0)
        self.assertFalse(r.ok)
        self.assertEqual(len(exits), 1)
        self.assertIn(LOOP_ERROR, codes(r))

    def test_exits_run_when_universe_refresh_raises_and_retry_is_throttled(self):
        eng, ex, feed, store = self.build()
        exits = self.record_exits(eng)
        with mock.patch("coin_sniper.engine.select_universe", side_effect=RuntimeError("bad data")):
            r1 = eng.run_once(T0)
            self.assertEqual(len(exits), 1)
            self.assertIn(UNIVERSE, codes(r1))
            self.assertFalse(r1.ok)
            self.assertEqual(store.load("universe", {}), {"date": "2026-11-17", "symbols": []})
            for i in range(1, 5):
                eng.run_once(T0 + 30 * i * SEC)
            self.assertEqual(calls_named(ex, "symbol_rules"), 1)
            self.assertEqual(len(exits), 5)
        r = eng.run_once(T0 + config.UNIVERSE_RETRY_SECONDS * SEC)
        self.assertEqual(calls_named(ex, "symbol_rules"), 2)
        self.assertNotIn(UNIVERSE, codes(r))

    def test_exits_run_when_candle_preparation_raises(self):
        eng, ex, feed, store = self.build()
        exits = self.record_exits(eng)

        def boom(now):
            raise RuntimeError("candles")
        eng._prepare_candles = boom
        r = eng.run_once(T0)
        self.assertEqual(len(exits), 1)
        self.assertFalse(r.ok)
        self.assertEqual(r.equity, 1000)                     # 7단계는 계속 실행된다

    def test_loop_error_blocks_entries_and_clears_after_clean_loop(self):
        eng, ex, feed, store = self.build()
        entries = []
        eng._manage_entries = lambda now: entries.append(now)
        eng.run_once(T0)
        self.assertEqual(len(entries), 1)
        original = eng._reconcile

        def boom():
            raise RuntimeError("x")
        eng._reconcile = boom
        r1 = eng.run_once(T0 + 30 * SEC)
        self.assertIn(LOOP_ERROR, codes(r1))
        self.assertFalse(r1.entry_allowed)
        self.assertEqual(len(entries), 1)                    # 진입 단계는 건너뛴다
        eng._reconcile = original
        r2 = eng.run_once(T0 + 60 * SEC)
        self.assertTrue(r2.ok)
        self.assertNotIn(LOOP_ERROR, codes(r2))
        self.assertTrue(r2.entry_allowed)
        self.assertEqual(len(entries), 2)

    def test_exits_run_before_universe_refresh_at_day_change(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        exits = self.record_exits(eng, ex)
        n = len(ex.calls)
        eng.run_once(utc(2026, 11, 18, 0, 0, 5))
        names = [name for name, _ in ex.calls[n:]]
        first_refresh = n + min(names.index("symbol_rules"), names.index("daily_candles"))
        self.assertEqual(len(exits), 1)
        self.assertLessEqual(exits[0], first_refresh)
        self.assertIn("balances", [name for name, _ in ex.calls[n:exits[0]]])

    # ---- fix round 1: 평가액 기록 보존 ------------------------------------------------
    def test_pending_equity_row_survives_restart(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)                                     # last_equity 1000

        def boom(now):
            raise RuntimeError("down")
        eng._prepare_candles = boom
        day2 = utc(2026, 11, 18, 1)
        eng.run_once(day2)                                   # 날짜는 넘어갔지만 기록 전에 실패
        self.assertEqual(store.load("equity_history", []), [])
        self.assertEqual(store.load("risk_state", {})["pending_equity_row"],
                         {"day": "2026-11-17", "equity": 1000.0})
        btc = trending_candles(70, 50000, 100, today=date(2026, 11, 18))
        ex.set_candles("BTCUSDT", btc)
        eng2 = Engine(ex, feed, store, Metrics(store.load("metrics", {})), eng.settings,
                      sleep=lambda _s: None)
        eng2.startup(day2 + 30 * SEC)
        eng2.run_once(day2 + 30 * SEC)
        self.assertEqual(store.load("equity_history", []),
                         [{"day": "2026-11-17", "equity": 1000.0, "btc": btc[-2]["close"]}])
        self.assertNotIn("pending_equity_row", store.load("risk_state", {}))

    def test_pending_equity_row_older_than_candles_gets_no_btc(self):
        old = utc(2026, 8, 1, 12)
        eng, ex, feed, store = make_engine(test=self)
        store.save("risk_state", {"starting_capital": 1000.0, "realized_pnl": 0.0, "peak_equity": 1000.0,
                                  "day": "2026-08-01", "day_open_equity": 1000.0,
                                  "segment_start": old.isoformat(), "last_loop_at": old.isoformat(),
                                  "last_equity": 1000.0})
        eng.startup(T0)
        eng.run_once(T0)
        self.assertEqual(store.load("equity_history", []),
                         [{"day": "2026-08-01", "equity": 1000.0, "btc": None}])

    def test_pending_equity_row_waits_while_candles_lack_its_day(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        stale = trending_candles(70, 50000, 100, today=date(2026, 11, 16))
        ex.set_candles("BTCUSDT", stale[:-1])                # 11-15까지만: 11-17 봉이 아직 없다
        eng.run_once(utc(2026, 11, 18, 1))
        self.assertEqual(store.load("equity_history", []), [])
        self.assertEqual(store.load("risk_state", {})["pending_equity_row"]["day"], "2026-11-17")

    # ---- fix round 1: 진행 중 봉 ---------------------------------------------------
    def test_candles_overwrite_forming_bar_with_live_quote(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T0)
        feed.set_quote("SOLUSDT", 123, open=110, high=125, low=105, volume=10)
        c = eng.candles("SOLUSDT", T0)
        self.assertEqual(len(c), 70)
        self.assertEqual(c[-1], {"date": "2026-11-17", "open": 110, "high": 125, "low": 105, "close": 123,
                                 "vol": 10, "quote_vol": 1230})
        self.assertNotEqual(eng._candles["SOLUSDT"][-1]["close"], 123)   # 캐시는 그대로

    def test_candles_append_forming_bar_when_rest_lacks_today(self):
        eng, ex, feed, store = self.build()
        ex.set_candles("SOLUSDT", trending_candles(70, 100, 1, today=date(2026, 11, 16)))
        eng.run_once(T0)
        feed.set_quote("SOLUSDT", 123, volume=10)
        c = eng.candles("SOLUSDT", T0)
        self.assertEqual(c[-1]["date"], "2026-11-17")
        self.assertEqual(c[-1]["close"], 123)
        self.assertEqual(c[-2]["date"], "2026-11-16")
        self.assertEqual(len(c), config.CANDLE_COUNT)
        self.assertEqual(eng._candles["SOLUSDT"][-1]["date"], "2026-11-16")

    def test_candles_unchanged_without_fresh_quote_for_today(self):
        eng, ex, feed, store = self.build()
        rest = trending_candles(70, 100, 1, today=date(2026, 11, 16))
        ex.set_candles("SOLUSDT", rest)
        eng.run_once(T0)
        feed.set_quote("SOLUSDT", 123, age=config.QUOTE_MAX_AGE_SECONDS + 1)
        self.assertEqual(eng.candles("SOLUSDT", T0), rest)
        feed.set_quote("SOLUSDT", 123, day="2026-11-16")
        self.assertEqual(eng.candles("SOLUSDT", T0), rest)

    # ---- fix round 2: 종목 규칙 미확보 시 진입 차단 ---------------------------------------
    def restart_with_todays_universe(self, positions=None, balances=None):
        eng, ex, feed, store = make_engine(test=self, balances=balances)
        store.save("universe", {"date": "2026-11-17", "symbols": sorted(DEFAULT_PRICES)})
        store.save("risk_state", {"starting_capital": 1000.0, "realized_pnl": 0.0, "peak_equity": 1000.0,
                                  "day": "2026-11-17", "day_open_equity": 1000.0,
                                  "segment_start": T0.isoformat(), "last_loop_at": T0.isoformat(),
                                  "last_equity": 1000.0})
        if positions is not None:
            store.save("positions", positions)
        eng.startup(T0)
        entries = []
        eng._manage_entries = lambda now: entries.append(now)
        return eng, ex, feed, store, entries

    def test_rules_raising_on_restart_blocks_entries_until_loaded(self):
        eng, ex, feed, store, entries = self.restart_with_todays_universe()

        def boom():
            raise ConnectionResetError("reset")
        ex.symbol_rules = boom
        r1 = eng.run_once(T0)
        self.assertIn(UNIVERSE, codes(r1))
        self.assertIn("규칙", r1.halts[0]["detail"])
        r2 = eng.run_once(T0 + 30 * SEC)                     # 재시도 간격 안
        self.assertIn(UNIVERSE, codes(r2))
        self.assertFalse(r2.entry_allowed)
        self.assertEqual(entries, [])
        del ex.symbol_rules
        r3 = eng.run_once(T0 + config.UNIVERSE_RETRY_SECONDS * SEC)
        self.assertNotIn(UNIVERSE, codes(r3))
        self.assertTrue(r3.entry_allowed)
        self.assertEqual(len(entries), 1)

    def test_rules_rate_limited_on_restart_blocks_entries_after_wait(self):
        eng, ex, feed, store, entries = self.restart_with_todays_universe()
        ex.fail_next("symbol_rules", ApiResult(False, kind="transient", error="HTTP 429", retry_after=60))
        r1 = eng.run_once(T0)
        self.assertIn(RATE_LIMIT, codes(r1))
        r2 = eng.run_once(T0 + 60 * SEC)                     # 대기는 끝났지만 규칙은 아직 없다
        self.assertNotIn(RATE_LIMIT, codes(r2))
        self.assertIn(UNIVERSE, codes(r2))
        self.assertFalse(r2.entry_allowed)
        self.assertEqual(entries, [])
        r3 = eng.run_once(T0 + config.UNIVERSE_RETRY_SECONDS * SEC)
        self.assertNotIn(UNIVERSE, codes(r3))
        self.assertTrue(r3.entry_allowed)
        self.assertEqual(len(entries), 1)

    def test_universe_halt_clears_when_reconcile_loads_rules(self):
        eng, ex, feed, store, entries = self.restart_with_todays_universe(
            positions={"SOLUSDT": position("SOLUSDT", 1.0, 100)}, balances={"USDT": 10000.0, "SOL": 1.0})
        ex.fail_next("symbol_rules", ApiResult(False, kind="transient", error="HTTP 503"))
        r1 = eng.run_once(T0)
        self.assertIn(UNIVERSE, codes(r1))
        r2 = eng.run_once(T0 + config.UNIVERSE_RETRY_SECONDS * SEC)   # 대조 단계가 규칙을 받는다
        self.assertIn("SOLUSDT", eng.rules)
        self.assertNotIn(UNIVERSE, codes(r2))
        self.assertTrue(r2.entry_allowed)

    def test_cold_start_rules_fetch_is_throttled_and_recon_fails_closed(self):
        eng, ex, feed, store, entries = self.restart_with_todays_universe(
            positions={"SOLUSDT": position("SOLUSDT", 1.0, 100)}, balances={"USDT": 10000.0, "SOL": 0.9995})
        ex.fail_next("symbol_rules", ApiResult(False, kind="transient", error="HTTP 503"), times=10)
        for i in range(5):
            r = eng.run_once(T0 + 30 * i * SEC)
            self.assertIn(RECON_MISMATCH, codes(r))         # 수량 단위를 모르면 오차를 허용하지 않는다
            self.assertIn(UNIVERSE, codes(r))
        self.assertEqual(calls_named(ex, "symbol_rules"), 1)
        self.assertEqual(entries, [])

    def test_entries_not_called_without_rules_even_without_halts(self):
        eng, ex, feed, store = self.build()
        entries = []
        eng._manage_entries = lambda now: entries.append(now)
        eng.run_once(T0)
        self.assertEqual(len(entries), 1)
        eng._refresh_universe_if_due = lambda now: None
        eng._derive_universe_halt = lambda now: None
        eng.rules = {}
        r = eng.run_once(T0 + 30 * SEC)
        self.assertEqual(r.halts, [])
        self.assertFalse(r.entry_allowed)
        self.assertFalse(eng.snapshot(T0 + 30 * SEC)["entry_allowed"])
        self.assertEqual(len(entries), 1)

    def test_state_survives_restart(self):
        eng, ex, feed, store = self.build(positions={"SOLUSDT": position("SOLUSDT", 1.0, 100)},
                                          balances={"USDT": 10000.0, "SOL": 1.0})
        feed.set_quote("SOLUSDT", 120)
        eng.run_once(T0)                                     # peak 1020
        feed.set_health(False, "down")
        eng.run_once(T0 + 30 * SEC)
        eng2 = Engine(ex, feed, store, Metrics(store.load("metrics", {})), eng.settings,
                      sleep=lambda _s: None)
        eng2.startup(T0 + 60 * SEC)
        self.assertEqual(eng2.positions, {"SOLUSDT": position("SOLUSDT", 1.0, 100)})
        self.assertEqual(eng2.risk_state["peak_equity"], 1020)
        self.assertTrue(eng2.halts.is_set(FEED_STALE))
        self.assertEqual(eng2.universe["date"], "2026-11-17")
        self.assertEqual(eng2.metrics.summary(T0, None)["loops_ok"], 2)
        eng2.run_once(T0 + 60 * SEC)
        self.assertEqual(calls_named(ex, "daily_candles"), 7)  # 오늘 목록은 다시 고르지 않는다
        # 규칙만 다시 받는다: 첫 엔진(대조용 1 + 선정 1) + 재시작 대조용 1
        self.assertEqual(calls_named(ex, "symbol_rules"), 3)
        self.assertIn("SOLUSDT", eng2.rules)


if __name__ == "__main__":
    unittest.main()
