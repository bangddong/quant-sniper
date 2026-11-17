import json
import unittest
from datetime import datetime, timedelta, timezone

from coin_sniper.config import METRICS_RETENTION_DAYS, POLL_INTERVAL_SECONDS
from coin_sniper.metrics import Metrics, append_equity, period_returns
from coin_sniper.models import ApiResult

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)


def day(offset, hour=12):
    return datetime(2026, 11, 17, hour, tzinfo=timezone.utc) + timedelta(days=offset)


class MetricsTest(unittest.TestCase):
    def test_api_failure_rate_by_kind(self):
        m = Metrics()
        for _ in range(8):
            m.record_api("/api/v3/order", ApiResult(ok=True), NOW)
        m.record_api("/api/v3/order", ApiResult(ok=False, kind="transient"), NOW)
        m.record_api("/api/v3/account", ApiResult(ok=False, kind="permanent"), NOW)
        s = m.summary(NOW, 1)
        self.assertEqual((s["api_total"], s["api_failed"], s["api_fail_rate"]), (10, 2, 20.0))
        self.assertEqual((s["api_transient"], s["api_permanent"]), (1, 1))
        m.record_api("/api/v3/account", ApiResult(ok=False, kind="fatal"), NOW)
        s = m.summary(NOW, 1)
        self.assertEqual((s["api_failed"], s["api_permanent"], s["api_transient"]), (3, 2, 1))
        endpoints = m.to_dict()["days"]["2026-11-17"]["endpoints"]
        self.assertEqual(endpoints["/api/v3/order"], {"total": 9, "failed": 1})
        self.assertEqual(endpoints["/api/v3/account"], {"total": 2, "failed": 2})

    def test_fail_rate_zero_when_no_calls(self):
        self.assertEqual(Metrics().summary(NOW, 1)["api_fail_rate"], 0.0)

    def test_summary_window_excludes_older_days(self):
        m = Metrics()
        m.record_order("sent", day(-2))
        m.record_order("sent", day(-1))
        m.record_order("filled", day(-1))
        m.record_order("sent", NOW)
        m.record_order("rejected", NOW)
        m.record_order("unconfirmed", NOW)
        m.record_order("partial", NOW)
        m.record_restart(day(-2))
        self.assertEqual(m.summary(NOW, 1)["orders_sent"], 1)
        self.assertEqual(m.summary(NOW, 2)["orders_sent"], 2)
        self.assertEqual(m.summary(NOW, 3)["orders_sent"], 3)
        self.assertEqual(m.summary(NOW, None)["orders_sent"], 3)
        self.assertEqual(m.summary(NOW, 2)["restarts"], 0)
        self.assertEqual(m.summary(NOW, None)["restarts"], 1)
        today = m.summary(NOW, 1)
        self.assertEqual((today["orders_rejected"], today["orders_unconfirmed"], today["orders_partial"]), (1, 1, 1))

    def test_retention_drops_days_older_than_400(self):
        m = Metrics()
        old = NOW - timedelta(days=METRICS_RETENTION_DAYS + 1)
        edge = NOW - timedelta(days=METRICS_RETENTION_DAYS)
        m.record_restart(old)
        m.record_restart(edge)
        self.assertIn(old.date().isoformat(), m.to_dict()["days"])
        m.record_restart(NOW)
        days = m.to_dict()["days"]
        self.assertNotIn(old.date().isoformat(), days)
        self.assertIn(edge.date().isoformat(), days)
        self.assertEqual(m.summary(NOW, None)["restarts"], 2)

    def test_uptime_from_completed_loops(self):
        m = Metrics()
        past = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
        for _ in range(1440):
            m.record_loop(True, past)
        m.record_loop(False, past)
        # now = 다음 날 00:00:00 UTC, days=2 -> 지난 하루(86400초) + 오늘 경과 0초
        midnight = datetime(2026, 11, 17, 0, 0, 0, tzinfo=timezone.utc)
        s = m.summary(midnight, 2)
        self.assertEqual(POLL_INTERVAL_SECONDS, 30)
        self.assertEqual((s["loops_ok"], s["loops_failed"]), (1440, 1))
        self.assertEqual(s["uptime_pct"], 50.0)
        self.assertEqual(m.summary(midnight, None)["uptime_pct"], 50.0)

    def test_uptime_counts_today_elapsed_only_and_caps(self):
        m = Metrics()
        now = datetime(2026, 11, 17, 1, 0, 0, tzinfo=timezone.utc)   # 3600초 경과
        for _ in range(60):
            m.record_loop(True, now)
        self.assertEqual(m.summary(now, 1)["uptime_pct"], 50.0)
        for _ in range(500):
            m.record_loop(True, now)
        self.assertEqual(m.summary(now, 1)["uptime_pct"], 100.0)
        self.assertEqual(Metrics().summary(datetime(2026, 11, 17, tzinfo=timezone.utc), 1)["uptime_pct"], 0.0)

    def test_add_seconds_and_feed_counters(self):
        m = Metrics()
        m.add_seconds("feed_down", 12.5, NOW)
        m.add_seconds("feed_down", 7.5, NOW)
        m.add_seconds("halt:drawdown", 60, NOW)
        m.add_seconds("halt:drawdown", 30, NOW)
        m.add_seconds("halt:feed_stale", 5, day(-1))
        m.add_seconds("offline", 90, NOW)
        m.record_feed_reconnect(NOW)
        m.record_feed_reconnect(NOW)
        s = m.summary(NOW, 1)
        self.assertEqual(s["feed_down_seconds"], 20.0)
        self.assertEqual(s["halt_seconds"], {"drawdown": 90})
        self.assertEqual(s["offline_seconds"], 90)
        self.assertEqual(s["feed_reconnects"], 2)
        self.assertEqual(m.summary(NOW, 2)["halt_seconds"], {"drawdown": 90, "feed_stale": 5})

    def test_roundtrip_json(self):
        m = Metrics()
        m.record_api("x", ApiResult(ok=False, kind="transient"), NOW)
        m.record_order("sent", NOW)
        m.record_loop(True, NOW)
        m.add_seconds("halt:fatal", 3, NOW)
        data = json.loads(json.dumps(m.to_dict()))
        restored = Metrics(data)
        self.assertEqual(restored.to_dict(), m.to_dict())
        self.assertEqual(restored.summary(NOW, None), m.summary(NOW, None))


class EquityTest(unittest.TestCase):
    def test_append_equity_replaces_same_day(self):
        h = append_equity([], "2026-11-02", 1000.0, 50000.0)
        h = append_equity(h, "2026-11-01", 990.0, 49000.0)
        h = append_equity(h, "2026-11-02", 1010.0, 51000.0)
        self.assertEqual(h, [
            {"day": "2026-11-01", "equity": 990.0, "btc": 49000.0},
            {"day": "2026-11-02", "equity": 1010.0, "btc": 51000.0},
        ])

    def test_period_returns_vs_btc_and_none_when_history_short(self):
        history = []
        for i in range(8):
            history = append_equity(history, f"2026-11-{i + 1:02d}", 1000.0 + 10 * i, 100.0 + i)
        # 마지막 행: equity 1070, btc 107
        r = period_returns(history, 1177.0, 128.4)
        self.assertEqual(r["1d"], {"bot": 10.0, "btc": 20.0})
        self.assertEqual(r["7d"], {"bot": round((1177 / 1010 - 1) * 100, 2), "btc": round((128.4 / 101 - 1) * 100, 2)})
        self.assertEqual(r["all"], {"bot": 17.7, "btc": 28.4})
        self.assertEqual(r["30d"], {"bot": None, "btc": None})
        self.assertEqual(period_returns([], 1000.0, 100.0)["1d"], {"bot": None, "btc": None})
        self.assertEqual(period_returns([], 1000.0, 100.0)["all"], {"bot": None, "btc": None})

    def test_period_returns_zero_base_is_none(self):
        r = period_returns([{"day": "2026-11-01", "equity": 0.0, "btc": 100.0}], 10.0, 110.0)
        self.assertEqual(r["1d"], {"bot": None, "btc": 10.0})


if __name__ == "__main__":
    unittest.main()
