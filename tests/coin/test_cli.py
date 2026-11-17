"""coin_bot.py(실행 명령) 테스트. 네트워크·실제 git·실제 .env·실제 sleep 없이 가짜 거래소/피드와 임시 상태 폴더로 검증한다."""
from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

import coin_bot
from coin_sniper.config import POLL_INTERVAL_SECONDS, load_settings
from coin_sniper.dashboard import PublishSchedule
from coin_sniper.exchange.fake import FakeExchange, FakeFeed
from coin_sniper.ledger import InstanceLock, StateStore
from coin_sniper.models import ApiResult
from coin_sniper.risk import account_value
from helpers import DEFAULT_PRICES, TODAY, make_settings, position, rule, trending_candles, utc

NOW = utc(2026, 11, 17, 10, 0, 0)
DEMO_ENV = {"MODE": "demo", "BINANCE_DEMO_API_KEY": "k-test", "BINANCE_DEMO_API_SECRET": "s-test"}


def world(balances: dict[str, float] | None = None) -> tuple[FakeExchange, FakeFeed]:
    rules = {s: rule(s, 0.00001 if s == "BTCUSDT" else 0.001) for s in DEFAULT_PRICES}
    ex = FakeExchange(rules, {"USDT": 10000.0} if balances is None else balances)
    feed = FakeFeed()
    for symbol, price in DEFAULT_PRICES.items():
        ex.set_price(symbol, price)
        ex.set_candles(symbol, trending_candles(70, price * 0.8, price * 0.003, TODAY))
        feed.set_quote(symbol, price, day=TODAY.isoformat())
    return ex, feed


class TempStateMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="coin-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_dir = self.tmp / "state" / "demo"
        self.store = StateStore(self.state_dir)

    def seed(self, positions: dict, realized: float = 0.0, capital: float = 1000.0, intents=None):
        self.store.save("positions", positions)
        self.store.save("trades", [])
        self.store.save("intents", intents or {})
        self.store.save("risk_state", {
            "starting_capital": capital, "realized_pnl": realized, "peak_equity": 1100.0,
            "day": "2026-11-17", "day_open_equity": 1050.0, "segment_start": "2026-11-01T00:00:00+00:00",
            "last_loop_at": "2026-11-17T09:59:30+00:00", "pending_equity_row": {"day": "2026-11-16", "equity": 1.0},
        })

    def snapshot_files(self) -> dict:
        return {p.name: p.read_bytes() for p in self.state_dir.glob("*.json")}


class ReconcileTests(TempStateMixin, unittest.TestCase):
    def test_reconcile_closes_positions_missing_on_exchange_as_uncounted(self):
        # 장부 X 1.0, 거래소 X 0 → 포지션 삭제, trades에 type "SELL", reason "수동 정리", counted False, net_pnl 0
        # risk_state.segment_start == now
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0)}, realized=20.0)
        ex, _ = world({"USDT": 10000.0})
        with redirect_stdout(io.StringIO()):
            removed = coin_bot.reconcile(self.store, ex, NOW, False)
        self.assertEqual(removed, ["SOLUSDT"])
        self.assertEqual(self.store.load("positions", None), {})
        trades = self.store.load("trades", None)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["type"], t["symbol"], t["reason"], t["counted"]), ("SELL", "SOLUSDT", "수동 정리", False))
        self.assertEqual((t["net_pnl"], t["gross_pnl"], t["net_return_pct"], t["qty"], t["price"]), (0, 0, 0, 1.0, 0))
        self.assertEqual(t["dust_writeoff"], 100.0)
        self.assertTrue(t["client_id"].startswith("manual-"))
        self.assertEqual(len(t["client_id"]), len("manual-") + 12)
        rs = self.store.load("risk_state", None)
        self.assertEqual(rs["segment_start"], NOW.isoformat())
        self.assertAlmostEqual(rs["realized_pnl"], 20.0 - 100.0)
        events = self.store.load("events", None)
        self.assertEqual(events[-1]["type"], "SYSTEM")
        self.assertEqual(events[-1]["reason"], "수동 정리: SOLUSDT")

    def test_reconcile_keeps_positions_that_match(self):
        sol = position("SOLUSDT", 1.0, 100.0)
        xrp = position("XRPUSDT", 50.0, 2.0)
        self.seed({"SOLUSDT": sol, "XRPUSDT": xrp})
        # SOL: 장부보다 1스텝 미만 적어도 일치(허용 오차), XRP: 잠김 포함 일치
        ex, _ = world({"USDT": 10000.0, "SOL": 0.9995, "XRP": 0.0})
        ex._account("XRP")[1] += 50   # 잠김(보호 주문) 수량
        before = self.snapshot_files()
        with redirect_stdout(io.StringIO()) as out:
            removed = coin_bot.reconcile(self.store, ex, NOW, False)
        self.assertEqual(removed, [])
        self.assertEqual(self.snapshot_files(), before)       # 아무것도 바꾸지 않는다
        self.assertIn("정리할 포지션이 없습니다", out.getvalue())

    def test_reconcile_keeps_matching_and_removes_missing_leaving_other_intents(self):
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0), "XRPUSDT": position("XRPUSDT", 50.0, 2.0)},
                  intents={"cs-d-en-bbb": {"client_id": "cs-d-en-bbb", "symbol": "ETHUSDT", "side": "BUY"}})
        ex, _ = world({"USDT": 10000.0, "XRP": 50.0})
        with redirect_stdout(io.StringIO()):
            removed = coin_bot.reconcile(self.store, ex, NOW, False)
        self.assertEqual(removed, ["SOLUSDT"])
        self.assertEqual(list(self.store.load("positions", None)), ["XRPUSDT"])
        self.assertEqual(self.store.load("positions", None)["XRPUSDT"], position("XRPUSDT", 50.0, 2.0))
        self.assertEqual(list(self.store.load("intents", None)), ["cs-d-en-bbb"])

    def test_virtual_cash_after_removal_is_not_higher_than_before(self):
        positions = {"SOLUSDT": position("SOLUSDT", 1.0, 100.0), "XRPUSDT": position("XRPUSDT", 50.0, 2.0)}
        self.seed(positions, realized=35.0)
        cash_before, _ = account_value(self.store.load("risk_state", None), positions, {s: 0.0 for s in positions})
        ex, _ = world({"USDT": 10000.0, "XRP": 50.0})
        with redirect_stdout(io.StringIO()):
            coin_bot.reconcile(self.store, ex, NOW, False)
        rs = self.store.load("risk_state", None)
        remaining = self.store.load("positions", None)
        cash_after, _ = account_value(rs, remaining, {s: 0.0 for s in remaining})
        self.assertAlmostEqual(cash_after, rs["starting_capital"] + rs["realized_pnl"]
                               - sum(p["cost"] for p in remaining.values()))
        self.assertLessEqual(cash_after, cash_before + 1e-9)
        self.assertAlmostEqual(cash_after, cash_before)

    def test_reconcile_reset_capital_restores_starting_capital(self):
        # realized_pnl 0, peak_equity == starting_capital
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0)}, realized=-42.0, capital=1000.0)
        ex, _ = world({"USDT": 10000.0})
        with redirect_stdout(io.StringIO()):
            removed = coin_bot.reconcile(self.store, ex, NOW, True, starting_capital=1500.0)
        self.assertEqual(removed, ["SOLUSDT"])
        rs = self.store.load("risk_state", None)
        self.assertEqual(rs["realized_pnl"], 0.0)
        self.assertEqual(rs["starting_capital"], 1500.0)
        self.assertEqual(rs["peak_equity"], rs["starting_capital"])
        self.assertIsNone(rs["day_open_equity"])
        self.assertIsNone(rs["pending_equity_row"])
        self.assertEqual(rs["segment_start"], NOW.isoformat())
        self.assertIn("시작 자본 재설정", self.store.load("events", None)[-1]["reason"])

    def test_reset_capital_without_given_capital_keeps_stored_capital(self):
        self.seed({}, realized=-42.0, capital=800.0)
        ex, _ = world({"USDT": 10000.0})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(coin_bot.reconcile(self.store, ex, NOW, True), [])
        rs = self.store.load("risk_state", None)
        self.assertEqual((rs["realized_pnl"], rs["peak_equity"], rs["starting_capital"]), (0.0, 800.0, 800.0))

    def test_reset_capital_refused_while_positions_remain(self):
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0)}, realized=-42.0)
        ex, _ = world({"USDT": 10000.0, "SOL": 1.0})
        before = self.snapshot_files()
        with redirect_stdout(io.StringIO()), self.assertRaises(coin_bot.ReconcileError) as ctx:
            coin_bot.reconcile(self.store, ex, NOW, True)
        self.assertIn("SOLUSDT", str(ctx.exception))
        self.assertEqual(self.snapshot_files(), before)

    def test_reconcile_aborts_when_balances_unavailable(self):       # 장부를 건드리지 않는다
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0)})
        ex, _ = world({"USDT": 10000.0})
        ex.fail_next("balances", ApiResult(False, kind="transient", error="HTTP 503"))
        before = self.snapshot_files()
        with redirect_stdout(io.StringIO()), self.assertRaises(coin_bot.ReconcileError) as ctx:
            coin_bot.reconcile(self.store, ex, NOW, False)
        self.assertIn("HTTP 503", str(ctx.exception))
        self.assertEqual(self.snapshot_files(), before)

    def test_rules_unavailable_uses_zero_tolerance_and_says_so(self):
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0)})
        ex, _ = world({"USDT": 10000.0, "SOL": 0.9995})       # 1스텝 미만 부족: 오차 0이면 불일치
        ex.fail_next("symbol_rules", ApiResult(False, kind="transient", error="HTTP 503"))
        with redirect_stdout(io.StringIO()) as out:
            removed = coin_bot.reconcile(self.store, ex, NOW, False)
        self.assertEqual(removed, ["SOLUSDT"])
        self.assertIn("허용 오차 0", out.getvalue())

    def test_missing_position_with_pending_intent_is_held_back(self):
        intent = {"client_id": "cs-d-ex-aaa", "symbol": "SOLUSDT", "side": "SELL", "qty": 1.0, "kind": "exit"}
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0)}, realized=5.0, intents={"cs-d-ex-aaa": intent})
        ex, _ = world({"USDT": 10000.0})
        before = self.snapshot_files()
        with redirect_stdout(io.StringIO()) as out:
            removed = coin_bot.reconcile(self.store, ex, NOW, False)
        self.assertEqual(removed, [])
        self.assertIn("보류", out.getvalue())
        self.assertIn("봇을 먼저 실행", out.getvalue())
        self.assertEqual(self.snapshot_files(), before)          # 포지션·의도·위험 상태 그대로

    def test_missing_position_with_protective_record_is_held_back(self):
        prot = {"client_id": "cs-d-pr-ccc", "state": "open", "stop_price": 95.0}
        self.seed({"SOLUSDT": dict(position("SOLUSDT", 1.0, 100.0), protective=prot)})
        ex, _ = world({"USDT": 10000.0})
        before = self.snapshot_files()
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(coin_bot.reconcile(self.store, ex, NOW, False), [])
        self.assertIn("보류", out.getvalue())
        self.assertEqual(self.snapshot_files(), before)

    def test_mixed_case_removes_only_clean_missing_position(self):
        prot = {"client_id": "cs-d-pr-ccc", "state": "canceled", "stop_price": 95.0}
        positions = {"SOLUSDT": dict(position("SOLUSDT", 1.0, 100.0), protective=prot),
                     "XRPUSDT": position("XRPUSDT", 50.0, 2.0),
                     "ADAUSDT": position("ADAUSDT", 100.0, 0.5)}
        intents = {"cs-d-ex-ddd": {"client_id": "cs-d-ex-ddd", "symbol": "ADAUSDT", "side": "SELL"}}
        self.seed(positions, realized=10.0, intents=intents)
        cash_before, _ = account_value(self.store.load("risk_state", None), positions, {s: 0.0 for s in positions})
        ex, _ = world({"USDT": 10000.0})                         # 셋 다 거래소에 없음
        with redirect_stdout(io.StringIO()):
            removed = coin_bot.reconcile(self.store, ex, NOW, False)
        self.assertEqual(removed, ["XRPUSDT"])
        left = self.store.load("positions", None)
        self.assertEqual(left, {"SOLUSDT": positions["SOLUSDT"], "ADAUSDT": positions["ADAUSDT"]})
        self.assertEqual(self.store.load("intents", None), intents)
        rs = self.store.load("risk_state", None)
        self.assertAlmostEqual(rs["realized_pnl"], 10.0 - 100.0)  # XRP 원가만
        cash_after, _ = account_value(rs, left, {s: 0.0 for s in left})
        self.assertAlmostEqual(cash_after, cash_before)
        self.assertEqual([t["symbol"] for t in self.store.load("trades", None)], ["XRPUSDT"])

    def test_reset_refused_when_held_back_positions_remain(self):
        prot = {"client_id": "cs-d-pr-ccc", "state": "open", "stop_price": 95.0}
        self.seed({"SOLUSDT": dict(position("SOLUSDT", 1.0, 100.0), protective=prot)})
        ex, _ = world({"USDT": 10000.0})
        before = self.snapshot_files()
        with redirect_stdout(io.StringIO()), self.assertRaises(coin_bot.ReconcileError):
            coin_bot.reconcile(self.store, ex, NOW, True)
        self.assertEqual(self.snapshot_files(), before)

    def test_reconcile_asks_and_aborts_on_no(self):
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0)})
        ex, _ = world({"USDT": 10000.0})
        before = self.snapshot_files()
        prompts = []
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(coin_bot.ReconcileAborted):
            coin_bot.reconcile(self.store, ex, NOW, False, confirm=lambda p: prompts.append(p) or False)
        self.assertEqual(prompts, ["진행할까요? [y/N] "])
        self.assertIn("SOLUSDT", out.getvalue())                # 계획을 먼저 보여 준다
        self.assertEqual(self.snapshot_files(), before)


class SuspectSymbolTests(TempStateMixin, unittest.TestCase):
    """폐기한 매수의 의심 종목 목록: reconcile이 보여 주고 같은 확인으로 비운다 (F5)."""

    def seed_suspects(self, positions=None):
        self.seed(positions or {})
        rs = self.store.load("risk_state", None)
        rs["suspect_symbols"] = [{"symbol": "SOLUSDT", "since": "2026-11-17T09:00:00+00:00"}]
        self.store.save("risk_state", rs)

    def test_reconcile_prints_and_clears_suspects_after_confirmation(self):
        self.seed_suspects()
        ex, _ = world({"USDT": 10000.0, "SOL": 1.0})
        prompts = []
        with redirect_stdout(io.StringIO()) as out:
            removed = coin_bot.reconcile(self.store, ex, NOW, False, confirm=lambda p: prompts.append(p) or True)
        self.assertEqual(removed, [])
        self.assertEqual(len(prompts), 1)
        self.assertIn("SOLUSDT", out.getvalue())
        rs = self.store.load("risk_state", None)
        self.assertEqual(rs["suspect_symbols"], [])
        self.assertEqual(rs["segment_start"], "2026-11-01T00:00:00+00:00")   # 구간은 그대로
        self.assertIn("SOLUSDT", self.store.load("events", None)[-1]["reason"])

    def test_reconcile_keeps_suspects_when_declined(self):
        self.seed_suspects()
        ex, _ = world({"USDT": 10000.0})
        before = self.snapshot_files()
        with redirect_stdout(io.StringIO()), self.assertRaises(coin_bot.ReconcileAborted):
            coin_bot.reconcile(self.store, ex, NOW, False, confirm=lambda p: False)
        self.assertEqual(self.snapshot_files(), before)

    def test_reconcile_clears_suspects_together_with_removal(self):
        self.seed_suspects({"XRPUSDT": position("XRPUSDT", 50.0, 2.0)})
        ex, _ = world({"USDT": 10000.0})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(coin_bot.reconcile(self.store, ex, NOW, False), ["XRPUSDT"])
        self.assertEqual(self.store.load("risk_state", None)["suspect_symbols"], [])


class SegmentTests(unittest.TestCase):
    """수익률 구간: reconcile/--reset-capital 뒤 기간별·주간 수익률이 이전 구간 줄과 섞이지 않는다 (F3)."""
    T = utc(2026, 11, 17, 12)

    def engine(self, **kw):
        from helpers import make_engine
        eng, ex, feed, store = make_engine(test=self, **kw)
        eng.startup(self.T)
        return eng, ex, feed, store

    def reconcile(self, eng, ex, at, reset):
        with redirect_stdout(io.StringIO()):
            coin_bot.reconcile(StateStore(eng.store.state_dir), ex, at, reset, starting_capital=1000.0)

    def test_reset_capital_starts_new_segment_without_fake_jump(self):
        from helpers import restart_engine
        eng, ex, feed, store = self.engine()
        eng.risk_state["realized_pnl"] = -150.0
        eng.equity_history = [{"day": f"2026-11-{d:02d}", "equity": 850.0, "btc": 80000.0} for d in range(1, 17)]
        eng.run_once(self.T)
        self.assertEqual(eng.equity, 850.0)
        self.reconcile(eng, ex, self.T + timedelta(seconds=60), True)
        eng2 = restart_engine(eng, self.T + timedelta(seconds=90))
        eng2.run_once(self.T + timedelta(seconds=90))
        self.assertEqual(eng2.equity, 1000.0)
        snap = eng2.snapshot(self.T + timedelta(seconds=90))
        for key in ("1d", "7d", "30d", "all"):
            self.assertIsNone(snap["returns"][key]["bot"], key)            # 850 → 1000 점프를 보이지 않는다
        self.assertEqual(snap["weekly_equity"], [])
        start = snap["returns"]["start"]
        self.assertEqual((start["kind"], start["bot"]), ("capital", 0.0))
        self.assertIn("| 시작 자본 대비 (2026-11-17부터) | +0.00% | - |", coin_bot.render_dashboard(snap))
        self.assertEqual(len(eng2.store.load("equity_history", [])), 16)   # 이전 기록은 지우지 않는다
        day2 = utc(2026, 11, 18, 0, 20)
        eng2.run_once(day2)                                                  # 새 구간의 첫 줄
        snap = eng2.snapshot(day2)
        self.assertEqual(snap["returns"]["1d"]["base_day"], "2026-11-17")
        self.assertEqual(snap["returns"]["1d"]["bot"], 0.0)

    def test_reconcile_without_reset_measures_from_segment_start_equity(self):
        from helpers import add_position, restart_engine
        eng, ex, feed, store = self.engine()
        add_position(eng, ex, "ETHUSDT", 0.1, 3000.0)
        eng.positions["SOLUSDT"] = position("SOLUSDT", 1.0, 100.0)          # 거래소에 없는 포지션
        eng.store.save("positions", eng.positions)
        eng.run_once(self.T)
        self.reconcile(eng, ex, self.T + timedelta(seconds=60), False)
        rs = eng.store.load("risk_state", {})
        self.assertEqual((rs["segment_kind"], rs["segment_base_equity"]), ("reconcile", None))
        eng2 = restart_engine(eng, self.T + timedelta(seconds=90))
        eng2.run_once(self.T + timedelta(seconds=90))
        self.assertAlmostEqual(eng2.equity, 900.0)                          # 1000 − SOL 원가 100
        self.assertAlmostEqual(eng2.risk_state["segment_base_equity"], 900.0)
        ex.set_price("ETHUSDT", 3300.0)
        feed.set_quote("ETHUSDT", 3300.0)
        eng2.run_once(self.T + timedelta(seconds=120))
        snap = eng2.snapshot(self.T + timedelta(seconds=120))
        self.assertAlmostEqual(snap["returns"]["start"]["bot"], (930.0 / 900.0 - 1) * 100)
        self.assertEqual(snap["returns"]["start"]["kind"], "reconcile")
        self.assertIn("| 구간 시작 대비 (2026-11-17부터) | +3.33% | - |", coin_bot.render_dashboard(snap))

    def test_reconcile_stamps_old_rows_with_previous_segment(self):
        eng, ex, feed, store = self.engine()
        first = eng.risk_state["segment_start"]
        eng.equity_history = [{"day": "2026-11-15", "equity": 990.0, "btc": 1.0}]
        eng.positions["SOLUSDT"] = position("SOLUSDT", 1.0, 100.0)
        eng.run_once(self.T)
        self.reconcile(eng, ex, self.T + timedelta(seconds=60), False)
        rows = eng.store.load("equity_history", [])
        self.assertEqual(rows, [{"day": "2026-11-15", "equity": 990.0, "btc": 1.0, "segment": first}])


class MainTests(TempStateMixin, unittest.TestCase):
    def call_main(self, argv, env=None, stdin=None):
        def settings_in_tmp(e):
            return dataclasses.replace(load_settings(e), state_dir=self.state_dir)
        out, err = io.StringIO(), io.StringIO()
        patches = [
            mock.patch.object(coin_bot, "load_dotenv", lambda _p: None),
            mock.patch.dict(os.environ, DEMO_ENV if env is None else env, clear=True),
            mock.patch.object(coin_bot, "load_settings", settings_in_tmp),
            mock.patch.object(coin_bot, "read_commit", lambda: "abc1234"),
            mock.patch.object(coin_bot, "BinanceExchange", lambda *a, **k: self.ex),
            mock.patch.object(coin_bot, "BinanceFeed", lambda *a, **k: self.feed),
            mock.patch("builtins.input", lambda _p="": stdin if stdin is not None else "n"),
        ]
        for p in patches:
            p.start()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = coin_bot.main(argv)
        finally:
            for p in reversed(patches):
                p.stop()
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        super().setUp()
        self.ex, self.feed = world()

    def test_run_refuses_without_keys(self):                         # 종료 코드 2
        code, _, err = self.call_main(["run"], env={"MODE": "demo"})
        self.assertEqual(code, 2)
        self.assertIn("키", err)
        self.assertEqual(self.ex.calls, [])

    def test_run_refuses_on_bad_settings(self):
        code, _, err = self.call_main(["run"], env={"MODE": "live", "BINANCE_LIVE_API_KEY": "x"})
        self.assertEqual(code, 2)
        self.assertIn("LIVE_TRADING_ENABLED", err)

    def test_run_refuses_on_corrupt_state(self):                     # 종료 코드 2, 원인 파일 이름 출력
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "positions.json").write_text("{not json", encoding="utf-8")
        code, _, err = self.call_main(["run"])
        self.assertEqual(code, 2)
        self.assertIn("positions.json", err)
        self.assertEqual(self.feed.started, 0)

    def test_run_refuses_when_already_running(self):
        lock = InstanceLock(self.state_dir / "coin_bot.lock")
        lock.acquire()
        try:
            code, _, err = self.call_main(["run"])
            self.assertEqual(code, 2)
            self.assertIn("이미 실행 중입니다", err)
            code, _, err = self.call_main(["reconcile", "--yes"])
            self.assertEqual(code, 2)
            self.assertIn("이미 실행 중입니다", err)
        finally:
            lock.release()
        self.assertEqual(self.ex.calls, [])

    def test_reconcile_command_confirms_and_exit_codes(self):
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0)})
        code, out, _ = self.call_main(["reconcile"], stdin="n")
        self.assertEqual(code, 1)
        self.assertIn("SOLUSDT", self.store.load("positions", None))
        code, out, _ = self.call_main(["reconcile"], stdin="y")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load("positions", None), {})
        self.assertIn("수동 정리", self.store.load("trades", None)[0]["reason"])
        lock = InstanceLock(self.state_dir / "coin_bot.lock")   # 끝나면 잠금이 풀려 있다
        lock.acquire()
        lock.release()

    def test_reconcile_command_ctrl_c_at_prompt_changes_nothing(self):
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0)})
        before = self.snapshot_files()

        def interrupt(_p=""):
            raise KeyboardInterrupt
        with mock.patch("builtins.input", interrupt):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(coin_bot, "load_dotenv", lambda _p: None), \
                    mock.patch.dict(os.environ, DEMO_ENV, clear=True), \
                    mock.patch.object(coin_bot, "load_settings",
                                      lambda e: dataclasses.replace(load_settings(e), state_dir=self.state_dir)), \
                    mock.patch.object(coin_bot, "BinanceExchange", lambda *a, **k: self.ex), \
                    redirect_stdout(out), redirect_stderr(err):
                code = coin_bot.main(["reconcile"])
        self.assertEqual(code, 1)
        self.assertIn("중단: 변경 없음", out.getvalue() + err.getvalue())
        after = {k: v for k, v in self.snapshot_files().items()}
        self.assertEqual(after, before)

    def test_reconcile_command_reset_refused_with_positions_exit_1(self):
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0)})
        self.ex._account("SOL")[0] += 1
        code, _, err = self.call_main(["reconcile", "--reset-capital", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("SOLUSDT", err)

    def test_reconcile_command_balances_failure_exit_1(self):
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0)})
        self.ex.fail_next("balances", ApiResult(False, kind="transient", error="HTTP 503"))
        code, _, err = self.call_main(["reconcile", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 503", err)
        self.assertIn("SOLUSDT", self.store.load("positions", None))

    def test_status_on_empty_dir(self):
        code, out, _ = self.call_main(["status"], env={"MODE": "demo"})
        self.assertEqual(code, 0)
        self.assertIn("아직 실행 기록이 없습니다", out)
        self.assertFalse(self.state_dir.exists())

    def test_status_on_populated_dir(self):
        self.seed({"SOLUSDT": position("SOLUSDT", 1.0, 100.0)}, realized=12.5,
                  intents={"cs-d-en-bbb": {"client_id": "cs-d-en-bbb", "symbol": "ETHUSDT", "side": "BUY"}})
        self.store.save("halts", {"halts": [{"code": "feed_stale", "detail": "시세 없음",
                                             "since": "2026-11-17T09:00:00+00:00"}]})
        code, out, _ = self.call_main(["status"], env={"MODE": "demo"})
        self.assertEqual(code, 0)
        for text in ("DEMO", "SOLUSDT", "100", "95", "+12.50", "feed_stale", "2026-11-17 18:00:00",
                     "2026-11-17 18:59:30", "ETHUSDT"):
            self.assertIn(text, out)
        self.assertNotIn("k-test", out)

    def test_status_on_corrupt_file(self):
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "risk_state.json").write_text("{", encoding="utf-8")
        code, _, err = self.call_main(["status"], env={"MODE": "demo"})
        self.assertEqual(code, 2)
        self.assertIn("risk_state.json", err)

    def test_run_via_main_completes_loops_and_releases_lock(self):
        real_run = coin_bot.run
        fake_run = mock.Mock(side_effect=lambda settings, **kw: real_run(
            settings, sleep=lambda _s: None, max_loops=1, publish_fn=lambda *a: True, **kw))
        with mock.patch.object(coin_bot, "run", fake_run):
            code, out, _ = self.call_main(["run"])
        fake_run.assert_called_once()
        self.assertEqual(self.feed.started, 1)
        self.assertEqual(code, 0)
        self.assertIn("DEMO", out)
        self.assertNotIn("k-test", out)
        self.assertNotIn("s-test", out)
        lock = InstanceLock(self.state_dir / "coin_bot.lock")
        lock.acquire()
        lock.release()


class RunLoopTests(TempStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ex, self.feed = world()
        self.ticks = [NOW + timedelta(seconds=30 * i) for i in range(1000)]
        self.sleeps: list[float] = []

    def now_fn(self):
        return self.ticks.pop(0)

    def go(self, auto_push=False, publish_fn=None, schedule=None, loops=3):
        settings = dataclasses.replace(make_settings(self.state_dir), auto_git_push=auto_push)
        out = io.StringIO()
        with redirect_stdout(out):
            code = coin_bot.run(settings, exchange=self.ex, feed=self.feed, sleep=self.sleeps.append,
                                now_fn=self.now_fn, clock=lambda: 100.0, max_loops=loops,
                                publish_fn=publish_fn or mock.Mock(return_value=True),
                                commit_fn=lambda: "abc1234", schedule=schedule)
        return code, out.getvalue()

    @staticmethod
    def status_lines(text: str) -> list[str]:
        return [line for line in text.splitlines() if " KST | " in line]

    def test_run_three_loops_prints_status_and_drains_reconnects(self):
        self.feed.take_reconnect_count = mock.Mock(side_effect=[2, 0, 1])
        publish = mock.Mock(return_value=True)
        code, out = self.go(publish_fn=publish)
        self.assertEqual(code, 0)
        lines = self.status_lines(out)
        self.assertEqual(len(lines), 3)
        self.assertIn("2026-11-17 19:00:30 KST", lines[0])      # 첫 시각은 startup이 쓴다
        self.assertIn("보유 0", lines[0])
        self.assertEqual(self.feed.started, 1)
        metrics = self.store.load("metrics", None)
        self.assertEqual(sum(d["feed_reconnects"] for d in metrics["days"].values()), 3)
        self.assertEqual(sum(d["loops_ok"] + d["loops_failed"] for d in metrics["days"].values()), 3)
        publish.assert_not_called()                              # AUTO_GIT_PUSH 꺼짐
        poll_sleeps = [s for s in self.sleeps if s >= 10]
        self.assertEqual(poll_sleeps, [POLL_INTERVAL_SECONDS] * 2)   # 마지막 루프 뒤에는 자지 않는다

    def test_sleep_subtracts_loop_time_and_never_negative(self):
        times = iter([0.0, 12.0, 100.0, 200.0])
        settings = make_settings(self.state_dir)
        with redirect_stdout(io.StringIO()):
            coin_bot.run(settings, exchange=self.ex, feed=self.feed, sleep=self.sleeps.append,
                         now_fn=self.now_fn, clock=lambda: next(times), max_loops=2,
                         publish_fn=mock.Mock(), commit_fn=lambda: "-")
        self.assertEqual([s for s in self.sleeps if s >= 1 or s == 0], [POLL_INTERVAL_SECONDS - 12.0])
        times = iter([0.0, 45.0, 100.0, 200.0])
        self.sleeps.clear()
        with redirect_stdout(io.StringIO()):
            coin_bot.run(settings, exchange=self.ex, feed=self.feed, sleep=self.sleeps.append,
                         now_fn=self.now_fn, clock=lambda: next(times), max_loops=2,
                         publish_fn=mock.Mock(), commit_fn=lambda: "-")
        self.assertIn(0.0, self.sleeps)
        self.assertTrue(all(s >= 0 for s in self.sleeps))

    def test_publish_only_when_auto_git_push(self):
        publish = mock.Mock(return_value=True)
        code, out = self.go(auto_push=True, publish_fn=publish)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(publish.call_count, 1)
        readme, state, events, repo_dir, work_dir = publish.call_args.args
        self.assertIn("[DEMO]", readme)
        self.assertEqual(state["commit"], "abc1234")
        self.assertIsInstance(events, list)
        self.assertEqual(Path(repo_dir), coin_bot.REPO_ROOT)
        self.assertEqual(Path(work_dir), self.state_dir / "dashboard")
        self.assertEqual(len(self.status_lines(out)), 3)

    def test_publish_failure_marks_schedule_failed_and_loop_continues(self):
        publish = mock.Mock(return_value=False)
        schedule = mock.Mock(wraps=PublishSchedule())
        code, out = self.go(auto_push=True, publish_fn=publish, schedule=schedule)
        self.assertEqual(code, 0)
        self.assertEqual(publish.call_count, 1)                  # 실패 뒤 5분은 다시 시도하지 않는다
        schedule.failed.assert_called_once()
        schedule.published.assert_not_called()
        self.assertIn("대시보드 게시 실패(매매는 계속)", out)
        self.assertEqual(len(self.status_lines(out)), 3)

    def test_publish_exception_does_not_stop_loop(self):
        publish = mock.Mock(side_effect=RuntimeError("boom"))
        schedule = mock.Mock(wraps=PublishSchedule())
        code, out = self.go(auto_push=True, publish_fn=publish, schedule=schedule)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.status_lines(out)), 3)
        self.assertIn("대시보드 게시 오류(매매는 계속): RuntimeError", out)
        self.assertNotIn("boom", out)
        schedule.failed.assert_called_once()
        schedule.published.assert_not_called()

    def test_banner_first_run_shows_env_capital_to_be_stored(self):
        settings = make_settings(self.state_dir, starting_capital=1500.0)
        out = io.StringIO()
        with redirect_stdout(out):
            coin_bot.run(settings, exchange=self.ex, feed=self.feed, sleep=lambda _s: None, now_fn=self.now_fn,
                         clock=lambda: 0.0, max_loops=1, publish_fn=mock.Mock(), commit_fn=lambda: "-")
        text = out.getvalue()
        self.assertIn("시작 자본(가상 계좌): 1,500.00 USDT (첫 실행: 이 값으로 저장)", text)
        self.assertNotIn("reset-capital", text)

    def test_banner_shows_stored_capital_and_notes_changed_setting(self):
        self.store.save("risk_state", {"starting_capital": 1000.0, "realized_pnl": 0.0, "peak_equity": None,
                                       "day": None, "day_open_equity": None,
                                       "segment_start": "2026-11-01T00:00:00+00:00", "last_loop_at": None})
        settings = make_settings(self.state_dir, starting_capital=1500.0)
        out = io.StringIO()
        with redirect_stdout(out):
            coin_bot.run(settings, exchange=self.ex, feed=self.feed, sleep=lambda _s: None, now_fn=self.now_fn,
                         clock=lambda: 0.0, max_loops=1, publish_fn=mock.Mock(), commit_fn=lambda: "-")
        text = out.getvalue()
        self.assertIn("시작 자본(가상 계좌): 1,000.00 USDT (구간 시작 2026-11-01)", text)
        self.assertIn("설정값 1,500.00 USDT는 `reconcile --reset-capital` 실행 시 반영됩니다", text)
        out = io.StringIO()
        with redirect_stdout(out):
            coin_bot.run(make_settings(self.state_dir, starting_capital=1000.0), exchange=self.ex, feed=self.feed,
                         sleep=lambda _s: None, now_fn=self.now_fn, clock=lambda: 0.0, max_loops=1,
                         publish_fn=mock.Mock(), commit_fn=lambda: "-")
        self.assertNotIn("reset-capital", out.getvalue())

    def test_critical_events_mark_schedule_critical(self):
        schedule = mock.Mock(wraps=PublishSchedule())
        self.feed.set_health(False, "끊김")                       # FEED_STALE → RISK 이벤트
        self.go(auto_push=True, schedule=schedule, loops=1)
        calls = schedule.mark_dirty.call_args_list
        self.assertTrue(calls)
        self.assertTrue(calls[0].kwargs.get("critical"))

    def test_events_printed_on_their_own_lines(self):
        self.feed.set_health(False, "끊김")
        code, out = self.go(loops=1)
        self.assertIn("RISK", out)
        self.assertIn("feed_stale", self.status_lines(out)[0])

    def test_corrupt_state_returns_2(self):
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "intents.json").write_text("[", encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            code, _ = self.go()
        self.assertEqual(code, 2)
        self.assertIn("intents.json", err.getvalue())

    def test_keyboard_interrupt_stops_cleanly(self):
        def sleep(seconds):
            if seconds >= 10:
                raise KeyboardInterrupt
        settings = make_settings(self.state_dir)
        out = io.StringIO()
        with redirect_stdout(out):
            code = coin_bot.run(settings, exchange=self.ex, feed=self.feed, sleep=sleep, now_fn=self.now_fn,
                                clock=lambda: 0.0, publish_fn=mock.Mock(), commit_fn=lambda: "-")
        self.assertEqual(code, 0)
        self.assertIn("중지합니다", out.getvalue())
        self.assertIsNotNone(self.store.load("risk_state", None)["last_loop_at"])

    def test_commit_is_set_on_engine_snapshot(self):
        publish = mock.Mock(return_value=True)
        self.go(auto_push=True, publish_fn=publish, loops=1)
        self.assertEqual(publish.call_args.args[1]["commit"], "abc1234")


class ReadCommitTests(unittest.TestCase):
    def test_read_commit_uses_git_with_timeout_and_devnull(self):
        runner = mock.Mock(return_value=mock.Mock(stdout="abc1234\n"))
        self.assertEqual(coin_bot.read_commit(runner), "abc1234")
        args, kwargs = runner.call_args
        self.assertEqual(args[0], ["git", "rev-parse", "--short", "HEAD"])
        self.assertEqual(kwargs["timeout"], 10)
        self.assertIs(kwargs["stdin"], coin_bot.subprocess.DEVNULL)

    def test_read_commit_failure_is_dash(self):
        self.assertEqual(coin_bot.read_commit(mock.Mock(side_effect=OSError("no git"))), "-")
        self.assertEqual(coin_bot.read_commit(mock.Mock(return_value=mock.Mock(stdout=""))), "-")


if __name__ == "__main__":
    unittest.main()
