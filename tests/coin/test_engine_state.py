import unittest
from datetime import date, timedelta

from coin_sniper import config
from coin_sniper.engine import Engine, LoopReport
from coin_sniper.halts import (
    ACCOUNT_FETCH, DAILY_LOSS, DRAWDOWN, FATAL, FEED_STALE, RATE_LIMIT, RECON_MISMATCH, UNIVERSE,
)
from coin_sniper.metrics import Metrics
from coin_sniper.models import ApiResult

from helpers import make_engine, position, trending_candles, utc

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
        self.assertEqual([row["day"] for row in store.load("equity_history", [])], ["2026-11-13"])

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

        def boom():
            raise ValueError("unexpected payload")
        ex.balances = boom
        r = eng.run_once(T0)
        self.assertFalse(r.ok)
        errors = [e for e in r.events if e["type"] == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("ValueError", errors[0]["reason"])
        self.assertEqual(eng.metrics.summary(T0, None)["loops_failed"], 1)
        self.assertEqual(store.load("events", [])[-1]["type"], "ERROR")
        self.assertEqual(store.load("metrics", {})["days"]["2026-11-17"]["loops_failed"], 1)

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
        self.assertEqual(calls_named(ex, "symbol_rules"), 2)   # 규칙만 다시 받는다
        self.assertIn("SOLUSDT", eng2.rules)


if __name__ == "__main__":
    unittest.main()
