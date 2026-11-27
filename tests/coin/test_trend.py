"""추세 전략(COIN_STRATEGY=trend): 설정, 순수 함수, 엔진 통합(주간 판단 → 매수·청산·비중 축소)."""
import dataclasses
import math
import unittest
from datetime import date, timedelta

from coin_sniper import config
from coin_sniper.config import SettingsError, load_settings
from coin_sniper.execution import TREND_EXIT_REASON, TREND_TRIM_REASON
from coin_sniper.halts import DRAWDOWN
from coin_sniper.risk import drawdown_controls, floor_to_step, size_trend_entry, time_stop_due, trend_trim_qty, update_stop
from coin_sniper.strategy import trend_target, trend_week

from helpers import add_position, make_engine, restart_engine, rule, trending_candles, utc

MONDAY = date(2026, 11, 23)
T_MON = utc(2026, 11, 23, 1)          # 진입 금지 15분 뒤
SEC = timedelta(seconds=1)


def noisy_candles(n: int, start: float, up: float, down: float, today: date = MONDAY) -> list[dict]:
    """+up, -down 을 번갈아 오가는 일봉 n개(마지막이 today 진행 중 봉)."""
    out, close = [], start
    for i in range(n):
        close *= (1 + up) if i % 2 == 0 else (1 - down)
        out.append({"date": (today - timedelta(days=n - 1 - i)).isoformat(), "open": close, "high": close * 1.01,
                    "low": close * 0.99, "close": close, "vol": 1000.0, "quote_vol": 5e7})
    return out


def expected_weight(candles: list[dict], today: str) -> float:
    closes = [c["close"] for c in candles if c["date"] < today]
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 30, len(closes))]
    m = sum(rets) / 30
    vol = math.sqrt(sum((r - m) ** 2 for r in rets) / 30) * math.sqrt(365)
    return min(config.TREND_MAX_WEIGHT, config.TREND_VOL_TARGET / vol)


class TrendSettingsTests(unittest.TestCase):
    def test_default_is_breakout_and_trend_selectable(self):
        self.assertEqual(load_settings({}).strategy, "breakout")
        self.assertEqual(load_settings({"COIN_STRATEGY": "Trend"}).strategy, "trend")

    def test_unknown_strategy_and_market_bypass_with_trend_rejected(self):
        with self.assertRaises(SettingsError):
            load_settings({"COIN_STRATEGY": "momentum"})
        with self.assertRaises(SettingsError):
            load_settings({"COIN_STRATEGY": "trend", "COIN_DEMO_IGNORE_MARKET_FILTER": "true"})


class TrendPureTests(unittest.TestCase):
    def test_week_is_last_monday(self):
        self.assertEqual(trend_week(utc(2026, 11, 23, 0, 5)), "2026-11-23")
        self.assertEqual(trend_week(utc(2026, 11, 29, 23)), "2026-11-23")   # 일요일
        self.assertEqual(trend_week(utc(2026, 11, 30, 0)), "2026-11-30")

    def test_rising_gives_vol_scaled_weight_from_closed_candles_only(self):
        c = noisy_candles(70, 100.0, 0.03, 0.02)
        w = trend_target(c, "2026-11-23")
        self.assertAlmostEqual(w, expected_weight(c, "2026-11-23"), places=9)
        self.assertGreater(w, 0.0)
        self.assertLess(w, config.TREND_MAX_WEIGHT)        # 상한에 걸리지 않는 값이어야 공식을 검증한다
        c[-1]["close"] = 1.0                               # 진행 중 봉은 보지 않는다
        self.assertAlmostEqual(trend_target(c, "2026-11-23"), w, places=9)

    def test_low_vol_capped_and_falling_is_zero(self):
        self.assertEqual(trend_target(trending_candles(70, 100.0, 0.5, MONDAY), "2026-11-23"), 0.5)
        self.assertEqual(trend_target(trending_candles(70, 200.0, -0.5, MONDAY), "2026-11-23"), 0.0)

    def test_unknown_when_yesterday_missing_or_history_short(self):
        c = trending_candles(70, 100.0, 0.5, MONDAY)
        self.assertIsNone(trend_target(c[:-2] , "2026-11-23"))   # 어제 봉 없음
        self.assertIsNone(trend_target(c[-20:], "2026-11-23"))   # 이력 부족

    def test_mid_week_uses_monday_information(self):
        c = noisy_candles(70, 100.0, 0.03, 0.02, today=date(2026, 11, 25))
        self.assertAlmostEqual(trend_target(c, "2026-11-23"), expected_weight(c, "2026-11-23"), places=9)

    def test_trend_stop_is_fixed_and_no_time_stop(self):
        pos = {"buy_price": 100.0, "highest_price": 100.0, "stop_loss_pct": 0.25, "trailing_gap_pct": 0.0,
               "stop_price": 75.0, "strategy": "trend", "entry_time": "2026-01-01T00:00:00+00:00"}
        stop, reason = update_stop(pos, 150.0)              # 보호·추적 기준을 훨씬 넘어도
        self.assertEqual((stop, reason), (75.0, "비상 손절"))
        self.assertFalse(time_stop_due(pos, utc(2026, 11, 23)))

    def test_trend_drawdown_halts_only_past_wide_limit(self):
        self.assertEqual(drawdown_controls(880.0, 1000.0, "breakout")[2:], (0.5, True))
        limit = config.TREND_EQUITY_HALT_DRAWDOWN_PCT
        self.assertEqual(drawdown_controls(1000.0 * (1 - limit) + 1, 1000.0, "trend")[2:], (1.0, False))
        self.assertTrue(drawdown_controls(1000.0 * (1 - limit), 1000.0, "trend")[3])

    def test_sizing_and_trim(self):
        r = rule("ETHUSDT")
        self.assertEqual(size_trend_entry(1000.0, 1000.0, 3000.0, 0.2, r), 0.066)
        self.assertEqual(size_trend_entry(1000.0, 90.0, 3000.0, 0.2, r), 0.03)      # 현금 한도
        self.assertEqual(size_trend_entry(1000.0, 1000.0, 3000.0, 0.004, r), 0.0)   # 최소 주문 금액 미만
        self.assertEqual(trend_trim_qty(0.1, 3000.0, 1000.0, 0.2, r), 0.0)          # 300 ≤ 200×1.5
        self.assertEqual(trend_trim_qty(0.11, 3000.0, 1000.0, 0.2, r), 0.043)       # 330 → 200


class TrendEngineTests(unittest.TestCase):
    def build(self, strategy="trend", candles=None):
        eng, ex, feed, store = make_engine(test=self, today=MONDAY)
        for sym, c in (candles or {}).items():
            ex.set_candles(sym, c)
        eng.settings = dataclasses.replace(eng.settings, strategy=strategy)
        eng.startup(T_MON)
        return eng, ex, feed, store

    @staticmethod
    def buys(ex):
        return [p for n, p in ex.calls if n == "market_buy"]

    def test_monday_plan_buys_both_with_emergency_stop_once(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T_MON)
        self.assertEqual(eng.universe["symbols"], ["BTCUSDT", "ETHUSDT"])
        plan = store.load("risk_state", {})["trend_plan"]
        self.assertEqual((plan["week"], plan["targets"]), ("2026-11-23", {"BTCUSDT": 0.5, "ETHUSDT": 0.5}))
        pos = store.load("positions", {})
        self.assertEqual(sorted(pos), ["BTCUSDT", "ETHUSDT"])
        for p in pos.values():
            self.assertEqual(p["strategy"], "trend")
            self.assertAlmostEqual(p["stop_price"], p["buy_price"] * 0.75)
            self.assertEqual(p["protective"]["state"], "open")
        self.assertAlmostEqual(pos["BTCUSDT"]["qty"] * 90000.0, 500.0, delta=1.0)
        for i in range(1, 4):
            eng.run_once(T_MON + 30 * i * SEC)
        self.assertEqual(len(self.buys(ex)), 2)

    def test_falling_symbol_not_bought_and_held_one_sold_by_weekly_exit(self):
        eng, ex, feed, store = self.build(candles={"ETHUSDT": trending_candles(70, 3600.0, -5.0, MONDAY)})
        add_position(eng, ex, "ETHUSDT", 0.05, 3200.0, strategy="trend", stop_loss_pct=0.25, stop_price=2400.0)
        eng.run_once(T_MON)                                 # 판단(청산 단계가 먼저라 매도는 다음 루프)
        eng.run_once(T_MON + 30 * SEC)
        trades = store.load("trades", [])
        sells = [t for t in trades if t["type"] == "SELL"]
        self.assertEqual([(t["symbol"], t["reason"]) for t in sells], [("ETHUSDT", TREND_EXIT_REASON)])
        self.assertNotIn("ETHUSDT", store.load("positions", {}))
        self.assertEqual([b["symbol"] for b in self.buys(ex)], ["BTCUSDT"])

    def test_no_rebuy_in_same_week_after_position_gone(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T_MON)
        eng.positions.pop("BTCUSDT")                        # 비상 손절 등으로 사라졌다고 본다
        for i in range(1, 4):
            eng.run_once(T_MON + 30 * i * SEC)
        self.assertEqual([b["symbol"] for b in self.buys(ex)], ["BTCUSDT", "ETHUSDT"])

    def test_trim_on_monday_only(self):
        btc = noisy_candles(70, 90000.0, 0.03, 0.02)
        btc = [dict(c, close=c["close"] * 90000.0 / btc[-1]["close"]) for c in btc]   # 오늘 가격 90000 근처
        for tag, start in (("monday", T_MON), ("tuesday", utc(2026, 11, 24, 1))):
            with self.subTest(tag):
                eng, ex, feed, store = self.build(candles={"BTCUSDT": btc})
                add_position(eng, ex, "BTCUSDT", 0.012, 45000.0, strategy="trend", stop_loss_pct=0.25,
                             stop_price=33750.0)            # 가치 1080 vs 목표 ≈ 1540 × w
                if tag == "tuesday":
                    feed.set_quote("BTCUSDT", 90000.0, day="2026-11-24")
                    feed.set_quote("ETHUSDT", 3000.0, day="2026-11-24")
                for i in range(3):
                    eng.run_once(start + 30 * i * SEC)
                sells = [t for t in store.load("trades", []) if t["type"] == "SELL"]
                if tag == "monday":
                    w = store.load("risk_state", {})["trend_plan"]["targets"]["BTCUSDT"]
                    self.assertLess(w * 1540 * config.TREND_TRIM_RATIO, 1080)   # 축소 조건이 실제로 성립하는 설정인지
                    self.assertEqual([t["reason"] for t in sells], [TREND_TRIM_REASON])
                    left = store.load("positions", {})["BTCUSDT"]
                    self.assertAlmostEqual(left["qty"] * 90000.0, w * 1540, delta=20.0)
                    self.assertEqual(left["protective"]["qty"], floor_to_step(left["qty"], 0.00001))   # 줄인 수량으로 다시 건다
                else:
                    self.assertEqual(sells, [])

    def test_breakout_position_keeps_its_rules_and_blocks_trend_buy(self):
        eng, ex, feed, store = self.build()
        add_position(eng, ex, "BTCUSDT", 0.001, 80000.0, strategy="rsi_recovery")
        eng.run_once(T_MON)
        self.assertEqual([b["symbol"] for b in self.buys(ex)], ["ETHUSDT"])
        self.assertTrue(store.load("positions", {})["BTCUSDT"]["stop_price"] > 80000.0)   # 수익 보호선(돌파 규칙)

    def test_breakout_mode_still_manages_leftover_trend_position(self):
        eng, ex, feed, store = self.build(strategy="breakout",
                                          candles={"ETHUSDT": trending_candles(70, 3600.0, -5.0, MONDAY)})
        add_position(eng, ex, "ETHUSDT", 0.05, 3200.0, strategy="trend", stop_loss_pct=0.25, stop_price=2400.0)
        eng.run_once(T_MON)
        eng.run_once(T_MON + 30 * SEC)
        sells = [t for t in store.load("trades", []) if t["type"] == "SELL"]
        self.assertEqual([t["reason"] for t in sells], [TREND_EXIT_REASON])

    def test_plan_survives_restart_without_duplicate_buys(self):
        eng, ex, feed, store = self.build()
        eng.run_once(T_MON)
        fresh = restart_engine(eng, T_MON + 60 * SEC)
        fresh.run_once(T_MON + 60 * SEC)
        self.assertEqual(len(self.buys(ex)), 2)

    def test_no_plan_no_orders(self):
        eng, ex, feed, store = self.build(candles={"BTCUSDT": trending_candles(70, 90000.0, 10.0, MONDAY)[:-3]})
        eng.run_once(T_MON)                                 # BTC 어제 봉 없음: 판단 못 함
        self.assertNotIn("trend_plan", store.load("risk_state", {}))
        self.assertEqual(self.buys(ex), [])
        self.assertEqual(eng.entries.not_evaluated_reason, "이번 주 추세 판단 없음")


if __name__ == "__main__":
    unittest.main()


class TrendDashboardTests(unittest.TestCase):
    def test_dashboard_shows_weekly_plan_and_trend_position(self):
        from coin_sniper.dashboard import render_dashboard
        eng, ex, feed, store = make_engine(test=self, today=MONDAY)
        eng.settings = dataclasses.replace(eng.settings, strategy="trend")
        eng.startup(T_MON)
        eng.run_once(T_MON)
        text = render_dashboard(eng.snapshot(T_MON))
        self.assertIn("추세 주간 판단 (2026-11-23 주)", text)
        self.assertIn("| BTCUSDT | 50.00% |", text)
        self.assertIn("추세 주간 판단(2026-11-23): BTCUSDT 50%, ETHUSDT 50%", text)
