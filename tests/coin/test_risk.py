import unittest
from datetime import datetime, timezone

from coin_sniper.models import SymbolRules
from coin_sniper.risk import (
    account_value, drawdown_controls, floor_to_step, floor_to_tick,
    position_open_risk, size_entry, time_stop_due, update_stop,
)

RULES = SymbolRules("XUSDT", "X", "USDT", True, 0.001, 0.01, 5.0)


class RiskTests(unittest.TestCase):
    def test_floor_helpers_never_round_up_and_avoid_float_noise(self):
        self.assertEqual(floor_to_step(0.123456, 0.001), 0.123)
        self.assertEqual(floor_to_step(0.3, 0.1), 0.3)
        self.assertEqual(floor_to_tick(93.019, 0.01), 93.01)

    def test_account_value_is_virtual(self):
        rs = {"starting_capital": 1000.0, "realized_pnl": 50.0}
        pos = {"A": {"qty": 2.0, "cost": 200.0}}
        self.assertEqual(account_value(rs, pos, {"A": 110.0}), (850.0, 1070.0))

    def test_account_value_missing_price_raises(self):
        rs = {"starting_capital": 1000.0, "realized_pnl": 0.0}
        with self.assertRaises(KeyError):
            account_value(rs, {"A": {"qty": 1.0, "cost": 10.0}}, {})

    def test_size_by_risk(self):
        self.assertEqual(size_entry(1000, 1000, 100.0, 0.07, 0.0, RULES), 1.0)

    def test_size_capped_by_allocation(self):
        self.assertEqual(size_entry(1000, 1000, 100.0, 0.01, 0.0, RULES), 3.0)

    def test_size_capped_by_cash(self):
        self.assertEqual(size_entry(1000, 50, 100.0, 0.07, 0.0, RULES), 0.5)

    def test_size_shrinks_to_remaining_open_risk(self):
        self.assertEqual(size_entry(1000, 1000, 100.0, 0.07, 14.0, RULES), 0.857)

    def test_size_zero_when_open_risk_full_or_below_min_notional(self):
        self.assertEqual(size_entry(1000, 1000, 100.0, 0.07, 20.0, RULES), 0.0)
        self.assertEqual(size_entry(1000, 4.0, 100.0, 0.07, 0.0, RULES), 0.0)

    def test_size_multiplier_halves_risk_and_allocation(self):
        self.assertEqual(size_entry(1000, 1000, 100.0, 0.07, 0.0, RULES, 0.5), 0.5)

    def test_size_zero_for_bad_inputs(self):
        nan, inf = float("nan"), float("inf")
        self.assertEqual(size_entry(0, 1000, 100.0, 0.07, 0.0, RULES), 0.0)
        self.assertEqual(size_entry(1000, -1, 100.0, 0.07, 0.0, RULES), 0.0)
        self.assertEqual(size_entry(1000, 1000, 0.0, 0.07, 0.0, RULES), 0.0)
        self.assertEqual(size_entry(1000, 1000, 100.0, 0.0, 0.0, RULES), 0.0)
        self.assertEqual(size_entry(1000, 1000, 100.0, 0.07, 0.0, RULES, 0.0), 0.0)
        self.assertEqual(size_entry(nan, 1000, 100.0, 0.07, 0.0, RULES), 0.0)
        self.assertEqual(size_entry(1000, inf, 100.0, 0.07, 0.0, RULES), 0.0)
        self.assertEqual(size_entry(1000, 1000, 100.0, nan, 0.0, RULES), 0.0)

    def test_open_risk_zero_once_stop_above_entry(self):
        self.assertEqual(position_open_risk({"qty": 2, "buy_price": 100, "stop_price": 93}), 14)
        self.assertEqual(position_open_risk({"qty": 2, "buy_price": 100, "stop_price": 101.5}), 0)

    def test_stop_ratchets_and_never_moves_down(self):
        pos = {"buy_price": 100.0, "stop_loss_pct": 0.07, "trailing_gap_pct": 0.05,
               "highest_price": 100.0, "stop_price": 93.0, "trailing_active": False}
        self.assertEqual(update_stop(pos, 105.9)[0], 93.0)
        self.assertEqual(update_stop(pos, 106.0)[0], 101.5)
        self.assertAlmostEqual(update_stop(pos, 112.0)[0], 106.4)
        self.assertTrue(pos["trailing_active"])
        self.assertAlmostEqual(update_stop(pos, 108.0)[0], 106.4)
        self.assertEqual(pos["highest_price"], 112.0)

    def test_stop_reasons_and_state_update(self):
        pos = {"buy_price": 100.0, "stop_loss_pct": 0.07, "trailing_gap_pct": 0.05,
               "highest_price": 100.0, "stop_price": 93.0, "trailing_active": False}
        self.assertEqual(update_stop(pos, 100.0), (93.0, "동적 손절"))
        self.assertEqual(update_stop(pos, 106.0)[1], "수익 보호 스탑")
        self.assertEqual(pos["stop_price"], 101.5)
        self.assertEqual(update_stop(pos, 112.0)[1], "고점 추적 트레일링 스탑")
        self.assertAlmostEqual(pos["stop_price"], 106.4)

    def test_stop_never_below_previous_stop(self):
        pos = {"buy_price": 100.0, "stop_loss_pct": 0.07, "trailing_gap_pct": 0.05,
               "highest_price": 100.0, "stop_price": 97.0, "trailing_active": False}
        self.assertEqual(update_stop(pos, 100.0)[0], 97.0)

    def test_drawdown_slows_then_halts(self):
        self.assertEqual(drawdown_controls(100, 100)[2:], (1.0, False))
        self.assertEqual(drawdown_controls(92, 100)[2:], (0.5, False))
        self.assertEqual(drawdown_controls(88, 100)[2:], (0.5, True))

    def test_drawdown_none_peak_uses_equity(self):
        self.assertEqual(drawdown_controls(100, None), (100.0, 0.0, 1.0, False))

    def test_is_tradable_amount_uses_floored_qty_and_min_notional(self):
        from coin_sniper.risk import is_tradable_amount
        r = SymbolRules(symbol="SOLUSDT", base="SOL", quote="USDT", trading=True,
                        step_size=0.001, tick_size=0.01, min_notional=5.0)
        self.assertTrue(is_tradable_amount(0.05, 100.0, r))          # 정확히 최소 금액
        self.assertFalse(is_tradable_amount(0.0509, 98.5, r))        # 내림 0.05 × 98.5 < 5 (내리기 전은 5.01)
        self.assertFalse(is_tradable_amount(0.0009, 1e6, r))         # 1스텝 미만
        self.assertFalse(is_tradable_amount(0.0, 100.0, r))
        self.assertFalse(is_tradable_amount(-1.0, 100.0, r))

    def test_time_stop_is_168_hours_from_entry(self):
        pos = {"entry_time": "2026-11-10T03:00:00+00:00"}
        self.assertFalse(time_stop_due(pos, datetime(2026, 11, 17, 2, 59, tzinfo=timezone.utc)))
        self.assertTrue(time_stop_due(pos, datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)))


if __name__ == "__main__":
    unittest.main()
