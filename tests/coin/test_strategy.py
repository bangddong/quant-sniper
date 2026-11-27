import unittest
from datetime import datetime, timezone

from coin_sniper.strategy import (
    calculate_atr, confirm_persistent_signals, day_progress, evaluate_signals,
    in_entry_blackout, is_favorable_market,
)


def candle(close, open_price=None, high=None, low=None, vol=1000):
    open_price = close if open_price is None else open_price
    return {
        "date": "2026-01-01",
        "open": open_price,
        "high": max(close, open_price) + 1 if high is None else high,
        "low": min(close, open_price) - 1 if low is None else low,
        "close": close,
        "vol": vol,
        "quote_vol": vol * close,
    }


def rising_market():
    return [candle(100 + i) for i in range(70)]


def vbo_candles(scale=1.0, half_spread=0.5, open_price=106.0, close_extra=0.5,
                curr_vol=1800):
    """29 완료봉 + 변동성 돌파 당일봉. 모든 가격은 scale 배."""
    candles = []
    for i in range(29):
        c = 100 + i * 0.2
        candles.append(candle(c * scale, high=(c + half_spread) * scale,
                              low=(c - half_spread) * scale, vol=1000))
    target = open_price + half_spread  # 전일 범위 = 2*half_spread, K = 0.5
    close = target + close_extra
    candles.append(candle(close * scale, open_price=open_price * scale,
                          high=(close + 0.1) * scale, low=(open_price - 0.5) * scale,
                          vol=curr_vol))
    return candles


class StrategyTests(unittest.TestCase):
    def test_market_regime_requires_price_above_rising_ma(self):
        self.assertTrue(is_favorable_market(rising_market()))
        falling = [candle(200 - i) for i in range(70)]
        self.assertFalse(is_favorable_market(falling))

    def test_rsi_waits_for_recovery_cross(self):
        closes = [130 - i for i in range(29)]
        candles = [candle(value, vol=1000) for value in closes]
        previous = closes[-1]
        candles.append(candle(previous + 6, open_price=previous + 1,
                              high=previous + 7, low=previous, vol=1800))
        signals = evaluate_signals({"AAAUSDT": candles}, rising_market(), 1.0)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["symbol"], "AAAUSDT")
        self.assertEqual(signals[0]["strategy"], "rsi_recovery")
        self.assertNotIn("take_profit_pct", signals[0])

    def test_vbo_requires_volume_confirmation(self):
        candles = vbo_candles(curr_vol=1200)
        self.assertEqual(evaluate_signals({"BBBUSDT": candles}, rising_market(), 1.0), [])
        candles[-1]["vol"] = 1800
        signals = evaluate_signals({"BBBUSDT": candles}, rising_market(), 1.0)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["strategy"], "vbo")
        self.assertEqual(signals[0]["entry_price"], candles[-1]["close"])

    def test_unfavorable_market_blocks_all_entries(self):
        candles = [candle(130 - i, vol=1000) for i in range(29)]
        candles.append(candle(107, open_price=103, high=108, low=102, vol=2000))
        falling = [candle(200 - i) for i in range(70)]
        self.assertEqual(evaluate_signals({"CCCUSDT": candles}, falling, 1.0), [])

    def test_market_filter_can_be_skipped(self):
        falling = [candle(200 - i) for i in range(70)]
        self.assertEqual(evaluate_signals({"BBBUSDT": vbo_candles()}, falling, 1.0), [])
        signals = evaluate_signals({"BBBUSDT": vbo_candles()}, falling, 1.0, require_favorable_market=False)
        self.assertEqual([s["strategy"] for s in signals], ["vbo"])

    def test_stop_and_trail_clamped_to_coin_ranges(self):
        low_vol = vbo_candles(half_spread=0.5)
        sig = evaluate_signals({"LOWUSDT": low_vol}, rising_market(), 1.0)[0]
        self.assertLess(sig["atr"] / sig["entry_price"], 0.02)
        self.assertEqual(sig["stop_loss_pct"], 0.04)
        self.assertEqual(sig["trailing_gap_pct"], 0.04)
        high_vol = vbo_candles(half_spread=10.0)
        sig = evaluate_signals({"HIGHUSDT": high_vol}, rising_market(), 1.0)[0]
        self.assertGreater(sig["atr"] / sig["entry_price"], 0.15)
        self.assertEqual(sig["stop_loss_pct"], 0.10)
        self.assertEqual(sig["trailing_gap_pct"], 0.08)

    def test_day_surge_over_15_percent_excluded(self):
        candles = vbo_candles(open_price=106.0, close_extra=0.5)
        self.assertEqual(len(evaluate_signals({"X": candles}, rising_market(), 1.0)), 1)
        surge = vbo_candles(open_price=106.0, close_extra=0.5)
        prev_close = surge[-2]["close"]
        close = prev_close * 1.18
        surge[-1].update(close=close, high=close + 0.1)
        self.assertEqual(evaluate_signals({"X": surge}, rising_market(), 1.0), [])

    def test_gap_is_not_a_filter(self):
        candles = vbo_candles()
        prev_close = candles[-2]["close"]
        open_price = prev_close * 1.05
        target = open_price + (candles[-2]["high"] - candles[-2]["low"]) * 0.5
        close = target + 0.5
        candles[-1].update(open=open_price, close=close, high=close + 0.1,
                           low=open_price - 0.2)
        signals = evaluate_signals({"GAPUSDT": candles}, rising_market(), 1.0)
        self.assertEqual(len(signals), 1)

    def test_zero_range_candle_does_not_divide_by_zero(self):
        candles = vbo_candles()
        candles[-1].update(open=107.0, high=107.0, low=107.0, close=107.0)
        self.assertEqual(evaluate_signals({"FLATUSDT": candles}, rising_market(), 1.0), [])

    def test_tiny_priced_coin_signals(self):
        candles = vbo_candles(scale=1.234e-7)
        signals = evaluate_signals({"PEPEUSDT": candles}, rising_market(), 1.0)
        self.assertEqual(len(signals), 1)
        self.assertGreater(signals[0]["atr"], 0)
        self.assertEqual(signals[0]["entry_price"], candles[-1]["close"])

    def test_rsi_takes_precedence_and_signals_sorted_by_score(self):
        closes = [130 - i for i in range(29)]
        rsi_c = [candle(v) for v in closes]
        prev = closes[-1]
        rsi_c.append(candle(prev + 6, open_price=prev + 1, high=prev + 7, low=prev, vol=1800))
        signals = evaluate_signals({"VBOUSDT": vbo_candles(), "RSIUSDT": rsi_c},
                                   rising_market(), 1.0)
        self.assertEqual([s["symbol"] for s in signals], ["RSIUSDT", "VBOUSDT"])

    def test_short_history_skipped(self):
        self.assertEqual(evaluate_signals({"S": vbo_candles()[-24:]}, rising_market(), 1.0), [])

    def test_calculate_atr_keeps_precision(self):
        candles = vbo_candles(scale=1e-7)
        self.assertGreater(calculate_atr(candles), 0)

    def test_confirm_persistent_signals(self):
        sig = {"symbol": "A", "strategy": "vbo"}
        confirmed, conf = confirm_persistent_signals([sig], {}, 3)
        self.assertEqual(confirmed, [])
        confirmed, conf = confirm_persistent_signals([sig], conf, 3)
        self.assertEqual(confirmed, [])
        confirmed, conf = confirm_persistent_signals([sig], conf, 3)
        self.assertEqual(confirmed, [sig])
        confirmed, conf = confirm_persistent_signals([], conf, 3)
        self.assertEqual((confirmed, conf), ([], {}))

    def test_day_progress_and_blackout(self):
        utc = timezone.utc
        self.assertEqual(day_progress(datetime(2026, 11, 17, 12, 0, tzinfo=utc)), 0.5)
        self.assertEqual(day_progress(datetime(2026, 11, 17, 0, 0, tzinfo=utc)), 0.0)
        self.assertTrue(in_entry_blackout(datetime(2026, 11, 17, 0, 14, tzinfo=utc)))
        self.assertFalse(in_entry_blackout(datetime(2026, 11, 17, 0, 15, tzinfo=utc)))


if __name__ == "__main__":
    unittest.main()
