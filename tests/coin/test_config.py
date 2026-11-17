import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coin_sniper.config import SettingsError, load_settings  # noqa: E402
from coin_sniper.models import OrderState  # noqa: E402


class SettingsTests(unittest.TestCase):
    def test_demo_is_default_and_isolated(self):
        cfg = load_settings({})
        self.assertEqual(cfg.mode, "demo")
        self.assertEqual(cfg.rest_base, "https://demo-api.binance.com")
        self.assertEqual(cfg.stream_base, "wss://demo-stream.binance.com/ws")
        self.assertTrue(str(cfg.state_dir).replace("\\", "/").endswith("state/demo"))

    def test_live_requires_explicit_enable(self):
        with self.assertRaises(SettingsError):
            load_settings({"MODE": "live", "BINANCE_LIVE_API_KEY": "k", "BINANCE_LIVE_API_SECRET": "s"})

    def test_dashboard_branch_from_env_with_default(self):
        self.assertEqual(load_settings({}).dashboard_branch, "dashboard")
        self.assertEqual(load_settings({"COIN_DASHBOARD_BRANCH": " coin-dashboard "}).dashboard_branch,
                         "coin-dashboard")

    def test_live_refused_with_only_shared_stock_bot_flag(self):
        with self.assertRaises(SettingsError) as ctx:          # 주식 봇의 플래그는 코인 봇을 실거래로 만들지 않는다
            load_settings({"MODE": "live", "LIVE_TRADING_ENABLED": "true",
                           "BINANCE_LIVE_API_KEY": "k", "BINANCE_LIVE_API_SECRET": "s"})
        self.assertIn("COIN_LIVE_TRADING_ENABLED", str(ctx.exception))

    def test_live_when_enabled(self):
        cfg = load_settings({"MODE": "live", "COIN_LIVE_TRADING_ENABLED": "true",
                             "BINANCE_LIVE_API_KEY": "k", "BINANCE_LIVE_API_SECRET": "s"})
        self.assertEqual(cfg.rest_base, "https://api.binance.com")
        self.assertEqual(cfg.stream_base, "wss://stream.binance.com:9443/ws")
        cfg.require_keys()

    def test_missing_or_placeholder_keys_rejected(self):
        cfg = load_settings({"BINANCE_DEMO_API_KEY": "your_key_here"})
        with self.assertRaises(SettingsError):
            cfg.require_keys()

    def test_unknown_mode_rejected(self):
        with self.assertRaises(SettingsError):
            load_settings({"MODE": "paper"})

    def test_secret_not_in_repr(self):
        cfg = load_settings({"BINANCE_DEMO_API_KEY": "KEY123", "BINANCE_DEMO_API_SECRET": "SECRET456"})
        self.assertNotIn("KEY123", repr(cfg))
        self.assertNotIn("SECRET456", repr(cfg))


class OrderStateTests(unittest.TestCase):
    def test_avg_price_and_final(self):
        o = OrderState("c", "BTCUSDT", "BUY", "PARTIALLY_FILLED", executed_qty=2.0, quote_qty=200.0)
        self.assertEqual(o.avg_price, 100.0)
        self.assertFalse(o.is_final)
        self.assertEqual(OrderState("c", "BTCUSDT", "BUY", "NEW").avg_price, 0.0)
        self.assertTrue(OrderState("c", "BTCUSDT", "BUY", "FILLED").is_final)


if __name__ == "__main__":
    unittest.main()
