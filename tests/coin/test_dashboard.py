import json
import subprocess
import unittest
from datetime import timedelta
from pathlib import Path
import shutil
import tempfile

from coin_sniper import config
from coin_sniper.config import Settings
from coin_sniper.dashboard import (
    PublishSchedule, build_state_json, performance_stats, publish, render_dashboard,
)
from coin_sniper.halts import RECON_MISMATCH

from helpers import make_engine, set_signal, utc

T0 = utc(2026, 11, 17, 12)
SEC = timedelta(seconds=1)
SECRET_KEY = "SENTINEL-KEY-7f3a9c"
SECRET_SECRET = "SENTINEL-SECRET-b81e04"


def summary(**over) -> dict:
    base = {"api_total": 120, "api_failed": 3, "api_fail_rate": 2.5, "api_transient": 2, "api_permanent": 1,
            "orders_sent": 4, "orders_rejected": 1, "orders_unconfirmed": 0, "orders_partial": 1,
            "feed_reconnects": 2, "feed_down_seconds": 75.0, "halt_seconds": {"feed_stale": 90.0, "daily_loss": 3600.0},
            "offline_seconds": 0.0, "loops_ok": 100, "loops_failed": 1, "uptime_pct": 99.1, "restarts": 1}
    base.update(over)
    return base


def sell(net_pct, net_pnl=None, counted=True, strategy="vbo", time="2026-11-17T01:00:00+00:00", **extra):
    t = {"time": time, "type": "SELL", "symbol": "SOLUSDT", "qty": 1.0, "price": 100.0, "quote_qty": 100.0,
         "fees": {"USDT": 0.1}, "strategy": strategy, "reason": "동적 손절", "client_id": f"c{net_pct}{time}",
         "entry_price": 100.0, "gross_pnl": 0.0, "net_pnl": net_pct if net_pnl is None else net_pnl,
         "net_return_pct": net_pct, "slippage_pct": 0.1, "counted": counted, "dust_writeoff": 0.0}
    t.update(extra)
    return t


def snapshot(**over) -> dict:
    """현실적인 스냅숏 하나(엔진 없이). 시각은 UTC 10:18:18 → KST 19:18:18."""
    snap = {
        "mode": "demo", "now": "2026-11-17T10:18:18+00:00", "status": "running", "entry_allowed": True,
        "halts": [], "feed_connected": True, "feed_error": "",
        "last_quote_at": "2026-11-17T10:18:10+00:00", "last_account_sync": "2026-11-17T10:18:05+00:00",
        "last_loop_at": "2026-11-17T10:18:00+00:00", "commit": "abc1234",
        "universe_date": "2026-11-17",
        "universe": [
            {"symbol": "SOLUSDT", "price": 100.0, "max_qty": 1.75, "eligible": True, "reason": ""},
            {"symbol": "XRPUSDT", "price": None, "max_qty": 0.0, "eligible": False, "reason": "실시간 시세 없음"},
        ],
        "signal_progress": [{"symbol": "SOLUSDT", "strategy": "vbo", "count": 2}],
        "equity": 1012.5, "cash": 812.5, "drawdown_pct": 1.2, "daily_return_pct": 0.35,
        "size_multiplier": 1.0, "open_risk_pct": 0.5, "free_slots": 2,
        "positions": [{"symbol": "ETHUSDT", "qty": 0.066, "buy_price": 3000.0, "price": 3030.0, "return_pct": 1.0,
                       "initial_stop": 2850.0, "stop_price": 2850.0, "protective": "on", "protective_state": "open",
                       "hours_left": 150.5, "trailing_active": False, "strategy": "vbo"}],
        "unmanaged_assets": [], "protective_stops_enabled": True,
        "trades": [sell(10), sell(-5), sell(2)],
        "events": [{"type": "BUY", "symbol": "ETHUSDT", "reason": "vbo", "timestamp": "2026-11-17T02:00:00+00:00",
                    "count": 1},
                   {"type": "SKIP", "symbol": "XRPUSDT", "reason": "실시간 시세 없음",
                    "timestamp": "2026-11-17T10:00:00+00:00", "count": 4}],
        "metrics": {"24h": summary(), "7d": summary(api_total=900), "all": summary(api_total=5000)},
        "returns": {"1d": {"bot": 0.35, "btc": -1.2}, "7d": {"bot": 1.25, "btc": 3.4},
                    "30d": {"bot": None, "btc": None}, "all": {"bot": 1.25, "btc": 3.4}},
        "weekly_equity": [{"day": "2026-11-15", "equity": 1009.0, "return_pct": 0.9}],
        "entries_not_evaluated_reason": None,
    }
    snap.update(over)
    return snap


class RenderTests(unittest.TestCase):
    def test_mode_banner_on_first_heading(self):
        self.assertEqual(render_dashboard(snapshot()).splitlines()[0], "# Coin Sniper Dashboard  [DEMO]")
        self.assertEqual(render_dashboard(snapshot(mode="live")).splitlines()[0], "# Coin Sniper Dashboard  [LIVE]")

    def test_all_spec_sections_present(self):
        md = render_dashboard(snapshot())
        names = ["현재 상태", "시스템 신선도", "오늘의 감시", "위험 관리", "보유 포지션", "수익률", "오류율", "최근 7일 이벤트"]
        headings = [line for line in md.splitlines() if line.startswith("#")]
        positions = []
        for name in names:
            found = [i for i, h in enumerate(headings) if name in h]
            self.assertTrue(found, name)
            positions.append(found[0])
        self.assertEqual(positions, sorted(positions))      # 스펙 8절 순서
        self.assertTrue(md.rstrip().endswith("> 인증정보와 계좌 식별정보는 기록하지 않습니다."))

    def test_times_rendered_in_kst(self):
        md = render_dashboard(snapshot())
        self.assertIn("2026-11-17 19:18:18 KST", md)
        self.assertIn("2026-11-17 19:18:10", md)            # 마지막 시세
        self.assertIn("2026-11-17 19:00:00", md)            # 이벤트 시각
        self.assertNotIn("10:18:18", md)

    def test_returns_table_has_btc_comparison(self):
        md = render_dashboard(snapshot())
        self.assertIn("| 기간 | 봇 | BTC 보유 |", md)
        self.assertIn("| 오늘 | +0.35% | -1.20% |", md)
        self.assertIn("| 7일 | +1.25% | +3.40% |", md)
        self.assertIn("| 30일 | - | - |", md)
        self.assertIn("| 전체 | +1.25% | +3.40% |", md)
        self.assertIn("실거래소와 유사하지만 같지 않음", md)
        self.assertIn("| 2026-11-15 | 1,009.00 | +0.90% |", md)   # 주간 평가액

    def test_error_rate_table_has_three_windows(self):
        md = render_dashboard(snapshot())
        self.assertIn("| 항목 | 24시간 | 7일 | 전체 |", md)
        for label in ("API 호출", "API 실패율", "일시적/확정적 실패", "주문 전송/거절/미확인/부분 체결",
                      "시세 재연결", "시세 끊김 시간", "가동률", "중단 시간", "재시작", "루프 예외"):
            self.assertTrue(any(line.startswith(f"| {label}") for line in md.splitlines()), label)
        self.assertIn("| API 호출 | 120 | 900 | 5,000 |", md)
        self.assertIn("| API 실패율 | 2.50% (3건) | 2.50% (3건) | 2.50% (3건) |", md)
        self.assertIn("| 주문 전송/거절/미확인/부분 체결 | 4 / 1 / 0 / 1 |", md)
        line = next(l for l in md.splitlines() if l.startswith("| 중단 시간"))
        self.assertLess(line.index("daily_loss"), line.index("feed_stale"))   # 긴 사유가 먼저

    def test_needs_manual_halt_shows_manual_cleanup_label(self):
        halt = {"code": RECON_MISMATCH, "detail": "계좌·장부 불일치: SOLUSDT", "since": "2026-11-17T09:00:00+00:00",
                "needs_manual": True}
        md = render_dashboard(snapshot(status="halted", entry_allowed=False, halts=[halt]))
        self.assertIn("수동 정리 필요", md)
        self.assertIn("python coin_bot.py reconcile", md)
        self.assertIn("2026-11-17 18:00:00", md)            # 중단 시작 시각(KST)
        self.assertIn("중단", md.split("## 1. 현재 상태", 1)[1].split("###", 1)[0])
        plain = render_dashboard(snapshot(halts=[dict(halt, needs_manual=False)]))
        self.assertNotIn("수동 정리 필요", plain)

    def test_not_evaluated_reason_shown(self):
        md = render_dashboard(snapshot(entries_not_evaluated_reason="시장 프록시 실시간 시세 없음"))
        self.assertIn("시장 프록시 실시간 시세 없음", md)

    def test_bot_only_stop_warning_when_protective_disabled(self):
        warning = "거래소 손절 주문 비활성: 봇이 꺼져 있으면 손절이 실행되지 않습니다"
        self.assertNotIn(warning, render_dashboard(snapshot()))
        pos = dict(snapshot()["positions"][0], protective="disabled", protective_state=None)
        md = render_dashboard(snapshot(protective_stops_enabled=False, positions=[pos]))
        self.assertIn(warning, md)
        self.assertIn("disabled", md)

    def test_canceled_protective_shows_cancel_check(self):
        pos = dict(snapshot()["positions"][0], protective="off", protective_state="canceled")
        md = render_dashboard(snapshot(positions=[pos]))
        row = next(l for l in md.splitlines() if l.startswith("| ETHUSDT"))
        self.assertIn("취소 확인 중", row)
        self.assertIn("150.5시간", row)

    def test_markdown_pipe_in_reason_is_escaped(self):
        ev = {"type": "RISK", "symbol": "SOLUSDT", "reason": "a|b\nc", "timestamp": "2026-11-17T10:00:00+00:00",
              "count": 1}
        halt = {"code": "fatal", "detail": "x|y", "since": "2026-11-17T09:00:00+00:00"}
        md = render_dashboard(snapshot(events=[ev], halts=[halt], status="halted"))
        self.assertIn("a\\|b c", md)
        self.assertIn("x\\|y", md)
        self.assertNotIn("a|b", md)

    def test_events_newest_first_capped_at_100_with_count(self):
        evs = [{"type": "SKIP", "symbol": f"S{i}", "reason": "r", "count": 1,
                "timestamp": (T0 - timedelta(minutes=i)).isoformat()} for i in range(150)]
        md = render_dashboard(snapshot(events=list(reversed(evs))))
        rows = [l for l in md.split("최근 7일 이벤트", 1)[1].splitlines() if l.startswith("| 2026")]
        self.assertEqual(len(rows), 100)
        self.assertIn("| S0 |", rows[0])
        self.assertIn("150", md.split("최근 7일 이벤트", 1)[1].splitlines()[0])

    def test_empty_snapshot_values_render_as_dash(self):
        md = render_dashboard(snapshot(equity=None, cash=None, drawdown_pct=None, daily_return_pct=None,
                                       open_risk_pct=None, positions=[], trades=[], weekly_equity=[],
                                       last_quote_at=None, signal_progress=[], universe=[]))
        self.assertIn("보유 종목 없음", md)

    def test_no_secrets_in_output(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        eng, ex, feed, store = make_engine(test=self, state_dir=tmp)
        eng.settings = Settings(mode="demo", rest_base="", stream_base="", api_key=SECRET_KEY,
                                api_secret=SECRET_SECRET, state_dir=tmp, starting_capital=1000.0,
                                auto_git_push=True)
        eng.startup(T0)
        eng.run_once(T0)
        snap = eng.snapshot(T0)
        md = render_dashboard(snap)
        state = json.dumps(build_state_json(snap), ensure_ascii=False)
        events = json.dumps(snap["events"], ensure_ascii=False)
        for text in (md, state, events):
            for secret in (SECRET_KEY, SECRET_SECRET, "Settings(", "api_key", "api_secret"):
                self.assertNotIn(secret, text)


class PerformanceStatsTests(unittest.TestCase):
    def test_performance_stats_excludes_uncounted_trades(self):
        stats = performance_stats([sell(10), sell(-100, counted=False), sell(-5)])
        self.assertEqual(stats["closed"], 2)
        self.assertEqual(stats["win_rate"], 50.0)
        self.assertAlmostEqual(stats["total_net_pnl"], 5.0)
        self.assertEqual(stats["max_loss_streak"], 1)

    def test_performance_stats_values(self):
        trades = [sell(10, time="2026-11-10T00:00:00+00:00", strategy="vbo"),
                  sell(-5, time="2026-11-11T00:00:00+00:00", strategy="rsi_recovery"),
                  sell(2, time="2026-11-12T00:00:00+00:00", strategy="vbo"),
                  {"time": "2026-11-09T00:00:00+00:00", "type": "BUY", "symbol": "SOLUSDT", "qty": 1.0,
                   "price": 100.0, "quote_qty": 100.0, "fees": {"SOL": 0.001, "BNB": 0.5}, "strategy": "vbo",
                   "slippage_pct": None}]
        stats = performance_stats(trades, 1000.0)
        self.assertEqual(stats["closed"], 3)
        self.assertEqual(stats["win_rate"], 66.7)
        self.assertEqual(stats["max_loss_streak"], 1)
        self.assertAlmostEqual(stats["profit_factor"], 12 / 5)
        self.assertAlmostEqual(stats["avg_win_pct"], 6.0)
        self.assertAlmostEqual(stats["avg_loss_pct"], -5.0)
        self.assertAlmostEqual(stats["total_net_pnl"], 7.0)
        self.assertAlmostEqual(stats["total_fees_usdt"], 0.3 + 0.001 * 100.0)   # BNB는 값 매기지 않음
        self.assertEqual(stats["fees_unpriced_assets"], ["BNB"])
        self.assertAlmostEqual(stats["avg_slippage_pct"], 0.1)
        self.assertAlmostEqual(stats["max_drawdown_pct"], 0.5)                 # 고점 10 → 5, 자본 1000
        self.assertEqual(stats["by_strategy"]["vbo"], {"closed": 2, "win_rate": 100.0, "avg_return_pct": 6.0})
        self.assertEqual(stats["by_strategy"]["rsi_recovery"]["win_rate"], 0.0)

    def test_loss_streak_follows_time_order(self):
        trades = [sell(-1, time="2026-11-13T00:00:00+00:00"), sell(5, time="2026-11-10T00:00:00+00:00"),
                  sell(-2, time="2026-11-11T00:00:00+00:00"), sell(-3, time="2026-11-12T00:00:00+00:00")]
        stats = performance_stats(trades)
        self.assertEqual(stats["max_loss_streak"], 3)

    def test_empty_trades(self):
        stats = performance_stats([])
        self.assertEqual(stats["closed"], 0)
        for key in ("win_rate", "avg_win_pct", "avg_loss_pct", "profit_factor", "avg_slippage_pct"):
            self.assertIsNone(stats[key], key)
        self.assertEqual((stats["max_loss_streak"], stats["total_net_pnl"], stats["total_fees_usdt"],
                          stats["max_drawdown_pct"], stats["by_strategy"]), (0, 0.0, 0.0, 0.0, {}))

    def test_no_losses_profit_factor_none(self):
        self.assertIsNone(performance_stats([sell(3)])["profit_factor"])


class StateJsonTests(unittest.TestCase):
    def test_state_json_excludes_raw_lists_and_serialises(self):
        state = build_state_json(snapshot())
        self.assertNotIn("trades", state)
        self.assertNotIn("events", state)
        for key in ("status", "halts", "positions", "universe", "signal_progress", "returns", "metrics",
                    "performance_stats", "equity", "mode", "now"):
            self.assertIn(key, state)
        self.assertEqual(state["performance_stats"]["closed"], 3)
        json.dumps(state)


class ScheduleTests(unittest.TestCase):
    def test_schedule_critical_is_immediate_dirty_waits_5min_heartbeat_30min(self):
        self.assertEqual((config.DASHBOARD_MIN_INTERVAL_SECONDS, config.DASHBOARD_HEARTBEAT_SECONDS), (300, 1800))
        s = PublishSchedule()
        self.assertTrue(s.due(0.0, False))                  # 한 번도 게시하지 않음
        s.published(0.0)
        self.assertFalse(s.due(10.0, False))
        s.mark_dirty(critical=True)
        self.assertTrue(s.due(10.0, False))                 # 즉시
        s.published(10.0)
        s.mark_dirty()
        self.assertFalse(s.due(309.0, False))
        self.assertTrue(s.due(310.0, False))                # 변화: 5분
        s.published(310.0)
        self.assertFalse(s.due(609.0, False))
        self.assertTrue(s.due(610.0, True))                 # 보유 중: 5분
        self.assertFalse(s.due(2109.0, False))
        self.assertTrue(s.due(2110.0, False))               # 하트비트: 30분


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.work = self.repo / "dash"
        self.addCleanup(shutil.rmtree, self.repo, True)

    def runner(self, outputs):
        self.calls = []

        def run(args, **kwargs):
            self.calls.append((list(args), kwargs))
            out = outputs.pop(0)
            if isinstance(out, BaseException):
                raise out
            return subprocess.CompletedProcess(args, 0, stdout=out + "\n", stderr="")
        return run

    def test_publish_runs_exact_git_sequence(self):
        run = self.runner(["b1", "b2", "b3", "tree1", "commit1", ""])
        ok = publish("# readme", {"a": 1}, [{"type": "BUY"}], self.repo, self.work, runner=run)
        self.assertTrue(ok)
        args = [c[0] for c in self.calls]
        self.assertEqual(args[0], ["git", "hash-object", "-w", str(self.work / "README.md")])
        self.assertEqual(args[1], ["git", "hash-object", "-w", str(self.work / "dashboard_events.json")])
        self.assertEqual(args[2], ["git", "hash-object", "-w", str(self.work / "dashboard_state.json")])
        self.assertEqual(args[3], ["git", "mktree"])
        self.assertEqual(self.calls[3][1]["input"],
                         "100644 blob b1\tREADME.md\n100644 blob b2\tdashboard_events.json\n"
                         "100644 blob b3\tdashboard_state.json\n")
        self.assertEqual(args[4], ["git", "commit-tree", "tree1", "-m", "dashboard: rolling 7-day snapshot"])
        self.assertEqual(args[5], ["git", "push", "-f", "origin", "commit1:refs/heads/dashboard"])
        for _, kwargs in self.calls:
            self.assertEqual(kwargs["cwd"], self.repo)
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["check"])
        self.assertEqual((self.work / "README.md").read_text(encoding="utf-8"), "# readme")
        self.assertEqual(json.loads((self.work / "dashboard_state.json").read_text(encoding="utf-8")), {"a": 1})
        self.assertEqual(json.loads((self.work / "dashboard_events.json").read_text(encoding="utf-8")),
                         [{"type": "BUY"}])

    def test_publish_failure_returns_false_without_raising(self):
        err = subprocess.CalledProcessError(1, ["git", "push"], stderr="fatal: token=SECRET")
        run = self.runner(["b1", "b2", "b3", "tree1", "commit1", err])
        self.assertFalse(publish("# r", {}, [], self.repo, self.work, runner=run))
        run = self.runner([FileNotFoundError("git")])
        self.assertFalse(publish("# r", {}, [], self.repo, self.work, runner=run))


class EngineSnapshotTests(unittest.TestCase):
    KEYS = {
        "mode": str, "now": str, "status": str, "entry_allowed": bool, "halts": list, "feed_connected": bool,
        "feed_error": str, "last_quote_at": str, "last_account_sync": str, "last_loop_at": str, "commit": str,
        "universe_date": str, "universe": list, "signal_progress": list, "equity": float, "cash": float,
        "drawdown_pct": float, "daily_return_pct": float, "size_multiplier": float, "open_risk_pct": float,
        "free_slots": int, "positions": list, "unmanaged_assets": list, "protective_stops_enabled": bool,
        "trades": list, "events": list, "metrics": dict, "returns": dict, "weekly_equity": list,
    }

    def test_snapshot_after_buy_and_exit_has_every_key(self):
        eng, ex, feed, store = make_engine(test=self)
        set_signal(ex, feed, "SOLUSDT")
        eng.startup(T0)
        eng.commit = "abc1234"
        for i in range(3):
            eng.run_once(T0 + 30 * i * SEC)
        self.assertIn("SOLUSDT", eng.positions)
        snap = eng.snapshot(T0 + 60 * SEC)
        for key, typ in self.KEYS.items():
            self.assertIn(key, snap)
            self.assertIsInstance(snap[key], typ, key)
        self.assertEqual(snap["status"], "running")
        self.assertEqual(snap["commit"], "abc1234")
        pos = snap["positions"][0]
        for key in ("symbol", "qty", "buy_price", "price", "return_pct", "initial_stop", "stop_price",
                    "protective", "protective_state", "hours_left", "trailing_active"):
            self.assertIn(key, pos)
        self.assertEqual(pos["protective"], "on")
        self.assertAlmostEqual(pos["hours_left"], config.MAX_HOLD_HOURS)          # 매수는 세 번째 루프(T0+60초)
        later = eng.snapshot(T0 + 60 * SEC + timedelta(hours=10))["positions"][0]
        self.assertAlmostEqual(later["hours_left"], config.MAX_HOLD_HOURS - 10)
        self.assertAlmostEqual(pos["initial_stop"], pos["buy_price"] * (1 - eng.positions["SOLUSDT"]["stop_loss_pct"]))
        self.assertEqual(set(snap["metrics"]), {"24h", "7d", "all"})
        self.assertEqual(set(snap["returns"]), {"1d", "7d", "30d", "all"})
        self.assertEqual(snap["free_slots"], config.MAX_POSITIONS - 1)
        self.assertGreater(snap["open_risk_pct"], 0)
        rows = {r["symbol"]: r for r in snap["universe"]}
        self.assertEqual(rows["SOLUSDT"]["reason"], "이미 보유 중")
        self.assertFalse(rows["SOLUSDT"]["eligible"])
        self.assertTrue(any(r["eligible"] and r["max_qty"] > 0 for r in snap["universe"]))
        self.assertIsNone(snap["entries_not_evaluated_reason"])
        # 청산: 가격을 손절선 아래로
        ex.set_price("SOLUSDT", 50.0)
        feed.set_quote("SOLUSDT", 50.0)
        eng.run_once(T0 + 90 * SEC)
        snap = eng.snapshot(T0 + 90 * SEC)
        self.assertEqual(snap["positions"], [])
        self.assertEqual([t["type"] for t in snap["trades"]], ["BUY", "SELL"])
        self.assertEqual({"BUY", "SELL"} & {e["type"] for e in snap["events"]}, {"BUY", "SELL"})
        md = render_dashboard(snap)
        self.assertIn("SOLUSDT", md)
        json.dumps(build_state_json(snap))

    def test_status_halted_and_error(self):
        eng, ex, feed, store = make_engine(test=self)
        eng.startup(T0)
        feed.set_health(False, "closed")
        eng.run_once(T0)
        snap = eng.snapshot(T0)
        self.assertEqual(snap["status"], "halted")
        self.assertFalse(snap["feed_connected"])
        self.assertEqual(snap["entries_not_evaluated_reason"], "진입 허용 조건 미충족")
        eng._reconcile = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        eng.run_once(T0 + 30 * SEC)
        self.assertEqual(eng.snapshot(T0 + 30 * SEC)["status"], "error")

    def test_canceled_protective_state_reported(self):
        eng, ex, feed, store = make_engine(test=self)
        eng.startup(T0)
        from helpers import add_position
        add_position(eng, ex, "SOLUSDT", 1.0, 100.0,
                     protective={"client_id": "x", "stop_price": 95.0, "limit_price": 94.0, "qty": 1.0,
                                 "state": "canceled"})
        pos = eng.snapshot(T0)["positions"][0]
        self.assertEqual((pos["protective"], pos["protective_state"]), ("off", "canceled"))
        self.assertIn("취소 확인 중", render_dashboard(eng.snapshot(T0)))

    def test_proxy_without_quote_gives_none_btc_returns(self):
        eng, ex, feed, store = make_engine(test=self)
        eng.startup(T0)
        eng.run_once(T0)
        eng.equity_history = [{"day": "2026-11-16", "equity": 1000.0, "btc": 80000.0}]
        feed._quotes.pop("BTCUSDT")
        snap = eng.snapshot(T0)
        self.assertIsNone(snap["returns"]["1d"]["btc"])
        self.assertIsNone(snap["last_quote_at"])

    def test_weekly_equity_sundays_with_week_return(self):
        eng, ex, feed, store = make_engine(test=self)
        eng.startup(T0)
        eng.equity_history = [{"day": "2026-11-01", "equity": 1000.0, "btc": 1.0},   # 일요일
                              {"day": "2026-11-02", "equity": 990.0, "btc": 1.0},
                              {"day": "2026-11-08", "equity": 1100.0, "btc": 1.0},   # 일요일
                              {"day": "2026-11-15", "equity": 1045.0, "btc": 1.0}]   # 일요일
        weeks = eng.snapshot(T0)["weekly_equity"]
        self.assertEqual([w["day"] for w in weeks], ["2026-11-01", "2026-11-08", "2026-11-15"])
        self.assertIsNone(weeks[0]["return_pct"])
        self.assertAlmostEqual(weeks[1]["return_pct"], 10.0)
        self.assertAlmostEqual(weeks[2]["return_pct"], -5.0)


if __name__ == "__main__":
    unittest.main()
