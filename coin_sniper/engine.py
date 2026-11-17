"""코인 봇 엔진 루프: 계좌, 날짜 변경, 감시 종목, 대조, 중단.

원칙: 확인하지 못한 것은 "모름"이다. 모르면 신규 주문을 하지 않는다(fail-closed).
시각은 항상 now 인자(UTC, timezone-aware)로 받는다. 하루 경계는 UTC 0시.
청산·보호 주문·의도 복구는 execution.Executor(Task 11), 진입은 entries.EntryManager(Task 12)에 있다.
각 단계는 따로 예외를 잡는다: 한 단계가 실패해도 다음 단계(특히 청산)는 실행되고 LOOP_ERROR가 진입을 막는다.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from coin_sniper.config import (
    CANDLE_COUNT, DAILY_KILL_SWITCH_PCT, DEFAULT_BAN_RETRY_SECONDS, MARKET_PROXY_SYMBOL,
    MAX_POSITIONS, POLL_INTERVAL_SECONDS, QUOTE_ASSET, QUOTE_MAX_AGE_SECONDS,
    RECON_ESCALATE_SECONDS, UNIVERSE_FETCH_PAUSE_SECONDS, UNIVERSE_MIN_COUNT,
    UNIVERSE_RETRY_SECONDS, Settings,
)
from coin_sniper.exchange.base import Exchange, Feed
from coin_sniper.entries import EntryManager
from coin_sniper.execution import Executor
from coin_sniper.halts import (
    ACCOUNT_FETCH, DAILY_LOSS, DRAWDOWN, FATAL, FEED_STALE, LOOP_ERROR, MAINTENANCE, RATE_LIMIT,
    RECON_MISMATCH, UNIVERSE,
    HaltBoard,
)
from coin_sniper.ledger import StateStore, record_event
from coin_sniper.metrics import Metrics, append_equity
from coin_sniper.models import ApiResult, SymbolRules
from coin_sniper.risk import account_value, drawdown_controls
from coin_sniper.strategy import in_entry_blackout
from coin_sniper.universe import is_excluded, select_universe


@dataclass
class LoopReport:
    now: datetime
    ok: bool
    equity: float | None
    cash: float | None
    entry_allowed: bool
    halts: list[dict]
    events: list[dict]                     # 이번 루프에서 새로 생긴 이벤트
    raw_signals: int = 0
    confirmed_signals: int = 0


class _RateLimited(Exception):
    """호출 한도 응답을 받았다: 이번 루프의 남은 단계(거래소 호출)를 멈춘다."""


def _day(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


def _dec(value: float) -> Decimal:
    return Decimal(repr(float(value)))


class Engine:
    def __init__(self, exchange: Exchange, feed: Feed, store: StateStore, metrics: Metrics,
                 settings: Settings, sleep: Callable[[float], None] = time.sleep):
        self.exchange = exchange
        self.feed = feed
        self.store = store
        self.metrics = metrics
        self.settings = settings
        self.sleep = sleep
        # 저장되는 상태 (startup에서 불러온다)
        self.halts = HaltBoard()
        self.positions: dict[str, dict] = {}
        self.intents: dict[str, dict] = {}
        self.risk_state: dict = {}
        self.universe: dict = {"date": None, "symbols": []}
        self.events: list[dict] = []
        self.equity_history: list[dict] = []
        # 메모리 상태
        self.rules: dict[str, SymbolRules] = {}            # 마지막으로 받은 종목 규칙
        self.balances: dict[str, tuple[float, float]] | None = None   # 이번 루프 조회값, 모르면 None
        self.equity: float | None = None                   # 이번 루프 평가액, 모르면 None
        self.cash: float | None = None
        self.drawdown: float | None = None
        self.size_multiplier = 1.0                         # Task 12 수량 산정용
        self.unmanaged_assets: list[str] = []
        self._candles: dict[str, list[dict]] = {}          # 그날의 일봉 캐시 (REST 원본)
        self._candles_day: str | None = None
        self._feed_symbols: list[str] | None = None
        self._universe_tried_at: datetime | None = None   # 마지막 선정 시도(재시도 간격 기준)
        self._rules_tried_at: datetime | None = None      # 마지막 규칙만 받기 시도
        self._universe_detail = ""                         # 마지막 선정 실패 사유
        self._loop_failed = False
        self._prev_loop_at: datetime | None = None
        self._now: datetime | None = None
        self._loop_events: list[dict] = []
        self.executor = Executor(self, passthrough=(_RateLimited,))   # 주문 절차(매수·매도 공통)
        self.entries = EntryManager(self)                  # 진입과 신호 연속 확인(메모리)

    # ---- 시작 ---------------------------------------------------------------------
    def startup(self, now: datetime) -> None:
        """상태를 불러온다. 손상된 파일은 StateCorruptError로 그대로 올린다(빈 장부로 시작하지 않는다)."""
        s = self.store
        self.positions = s.load("positions", {})
        self.intents = s.load("intents", {})
        self.risk_state = s.load("risk_state", {})
        self.universe = s.load("universe", {"date": None, "symbols": []})
        self.events = s.load("events", [])
        self.equity_history = s.load("equity_history", [])
        self.halts = HaltBoard.from_dict(s.load("halts", {}))
        if not self.risk_state:
            self.risk_state = {
                "starting_capital": float(self.settings.starting_capital), "realized_pnl": 0.0,
                "peak_equity": None, "day": None, "day_open_equity": None,
                "segment_start": now.isoformat(), "last_loop_at": None,
            }
        self.feed.start()
        self.metrics.record_restart(now)
        last = self.risk_state.get("last_loop_at")
        if last:
            self._prev_loop_at = datetime.fromisoformat(last)
            gap = (now - self._prev_loop_at).total_seconds()
            if gap > 2 * POLL_INTERVAL_SECONDS:
                self.metrics.add_seconds("offline", gap, now)

    # ---- 루프 ---------------------------------------------------------------------
    def run_once(self, now: datetime) -> LoopReport:
        self._now = now
        self._loop_events = []
        self._loop_failed = False
        self.entries.begin_loop()
        ok = self._guarded(self._steps, now) and not self._loop_failed
        self.entries.end_loop()                            # 평가하지 않은 루프면 연속 확인을 처음부터
        if ok:
            self.risk_state["last_loop_at"] = now.isoformat()
        ok = self._guarded(self._save_state) and ok
        self.metrics.record_loop(ok, now)
        self._guarded(lambda: self.store.save("metrics", self.metrics.to_dict()))
        return LoopReport(now=now, ok=ok, equity=self.equity, cash=self.cash,
                          entry_allowed=self.entry_allowed(now), halts=self.halts.active(),
                          events=list(self._loop_events), raw_signals=self.entries.raw,
                          confirmed_signals=self.entries.confirmed)

    def _steps(self, now: datetime) -> None:
        self.balances = None
        self.equity = None
        self._account_halt_time(now)
        until = self.risk_state.get("rate_limit_until")
        if until and now < datetime.fromisoformat(until):
            return                                         # 대기 중: 거래소를 전혀 부르지 않는다
        self.risk_state["rate_limit_until"] = None
        self._clear_halt(RATE_LIMIT)
        steps = (
            ("intents", self._resolve_intents, (now,)),            # 1 (Task 11)
            ("day", self._roll_day, (now,)),                       # 2 날짜 장부만
            ("account", self._sync_account, ()),                   # 3
            ("reconcile", self._reconcile, ()),                    # 4
            ("exits", self._manage_exits, (now,)),                 # 5 (Task 11)
            ("universe", self._refresh_universe_if_due, (now,)),   # 6 감시 종목
            ("candles", self._prepare_candles, (now,)),            # 6 캔들 + 평가액 기록
            ("equity", self._evaluate_equity, ()),                 # 7
        )
        limited = False
        for name, fn, args in steps:
            if not self._run_step(name, fn, *args):
                limited = True                             # 호출 한도: 남은 단계는 거래소를 부르지 않는다
                break
        self._run_step("universe_halt", self._derive_universe_halt, now)   # 거래소 호출 없음: 항상
        if limited:
            return
        if not self._loop_failed:
            self._clear_halt(LOOP_ERROR)
        if self.entry_allowed(now):
            self._run_step("entries", self._manage_entries, now)   # 8 (Task 12)

    def _run_step(self, name: str, fn: Callable, *args) -> bool:
        """한 단계를 실행한다. 예외는 기록하고 계속, 호출 한도면 False(루프 중단)."""
        try:
            fn(*args)
        except _RateLimited:
            return False
        except Exception as exc:                           # KeyboardInterrupt/SystemExit는 잡지 않는다
            self._loop_failed = True
            self._event("ERROR", f"{name}: {type(exc).__name__}: {exc}")
            self._set_halt(LOOP_ERROR, f"루프 단계 오류({name}): {type(exc).__name__}")
        return True

    def _guarded(self, fn: Callable, *args) -> bool:
        try:
            fn(*args)
            return True
        except Exception as exc:
            self._event("ERROR", f"{type(exc).__name__}: {exc}")
            return False

    # ---- 이후 작업이 채우는 자리 -----------------------------------------------------
    def _resolve_intents(self, now: datetime) -> None:
        """1단계: 진행 중 주문 의도 해소."""
        self.executor.resolve_intents(now)

    def _manage_exits(self, now: datetime) -> None:
        """5단계: 청산과 보호 주문 관리. 앞 단계가 실패해도 불린다:
        self.balances·equity·캔들이 없을 수 있다. positions, rules(마지막 성공값), feed만 믿는다."""
        self.executor.manage_exits(now)

    def _manage_entries(self, now: datetime) -> None:
        """8단계: 진입. entry_allowed(now)일 때만 불린다."""
        self.entries.run(now)

    # ---- 거래소 호출 ----------------------------------------------------------------
    def _call(self, name: str, *args) -> ApiResult:
        """모든 거래소 호출의 관문. 호출 한도면 RATE_LIMIT를 걸고 _RateLimited, 확정 오류면 FATAL."""
        res = getattr(self.exchange, name)(*args)
        if res.ok:
            return res
        limited = res.retry_after > 0 or (
            res.kind == "transient" and res.error.startswith(("HTTP 429", "HTTP 418")))
        if limited:
            wait = res.retry_after if res.retry_after > 0 else float(DEFAULT_BAN_RETRY_SECONDS)
            self.risk_state["rate_limit_until"] = (self._now + timedelta(seconds=wait)).isoformat()
            self._set_halt(RATE_LIMIT, f"호출 한도({name}): {wait:.0f}초 대기")
            raise _RateLimited()
        if res.kind == "fatal":
            self._set_halt(FATAL, f"{name}: {res.error}")
        return res

    # ---- 2단계: 날짜 장부 -----------------------------------------------------------
    def _roll_day(self, now: datetime) -> None:
        today = _day(now)
        rs = self.risk_state
        if rs.get("day") == today:
            return
        if rs.get("day") and rs.get("last_equity") is not None:
            old = rs.get("pending_equity_row")
            if old:                                        # 아직 못 쓴 줄은 BTC 없이 남긴다
                self._append_equity_row(old["day"], old["equity"], None)
            rs["pending_equity_row"] = {"day": rs["day"], "equity": float(rs["last_equity"])}
        rs["day"] = today
        rs["day_open_equity"] = None                       # 오늘 첫 평가액으로 7단계에서 정한다
        self._clear_halt(DAILY_LOSS)

    # ---- 6단계: 감시 종목 -----------------------------------------------------------
    def _refresh_universe_if_due(self, now: datetime) -> None:
        today = _day(now)
        if self.universe.get("date") == today and self.universe.get("symbols"):
            self._ensure_rules(now)                        # 재시작: 오늘 목록은 있고 규칙만 없을 수 있다
            return
        tried = self._universe_tried_at
        if (tried is None or _day(tried) != today
                or (now - tried).total_seconds() >= UNIVERSE_RETRY_SECONDS):
            self._refresh_universe(now, today)

    def _ensure_rules(self, now: datetime) -> bool:
        """전체 선정 밖에서 symbol_rules()를 부르는 유일한 곳. 재시도 간격을 지키고 예외를 삼킨다
        (호출 한도는 _call대로 올린다). 실패하면 self.rules는 그대로 두고 False."""
        if self.rules:
            return True
        tried = self._rules_tried_at
        if tried is not None and (now - tried).total_seconds() < UNIVERSE_RETRY_SECONDS:
            return False
        self._rules_tried_at = now
        try:
            res = self._call("symbol_rules")
            if not res.ok:
                return False
            self.rules = dict(res.value)
        except _RateLimited:
            raise
        except Exception as exc:
            self._event("ERROR", f"rules: {type(exc).__name__}: {exc}")
            return False
        return True

    def _derive_universe_halt(self, now: datetime) -> None:
        """UNIVERSE는 상태에서 매 루프 다시 정한다: 오늘 목록이 있고 규칙이 있을 때만 해제."""
        today = _day(now)
        if self.universe.get("date") != today:
            reason = f"감시 종목 날짜 {self.universe.get('date')} ≠ 오늘 {today}"
        elif not self.universe.get("symbols"):
            reason = f"오늘 감시 종목 없음: {self._universe_detail or '선정 전'}"
        elif not self.rules:
            reason = "종목 규칙 미확보"
        else:
            self._clear_halt(UNIVERSE)
            return
        self._set_halt(UNIVERSE, reason)

    def _refresh_universe(self, now: datetime, today: str) -> None:
        self._universe_tried_at = now
        try:
            symbols, detail = self._select_universe(today)
        except Exception as exc:                           # 호출 한도 포함: 실패로 남기고 다시 올린다
            self._universe_failed(today, f"감시 종목 선정 오류: {type(exc).__name__}")
            raise
        if len(symbols) >= UNIVERSE_MIN_COUNT:
            self.universe = {"date": today, "symbols": symbols}
            self._universe_detail = ""
        else:
            self._universe_failed(today, detail)

    def _universe_failed(self, today: str, detail: str) -> None:
        self.universe = {"date": today, "symbols": []}     # 이전 목록으로 대체하지 않는다
        self._universe_detail = detail                     # 중단 자체는 _derive_universe_halt가 건다

    def _select_universe(self, today: str) -> tuple[list[str], str]:
        res = self._call("symbol_rules")
        if not res.ok:
            return [], f"종목 규칙 조회 실패: {res.error}"
        self.rules = dict(res.value)
        known_bases = frozenset(r.base for r in self.rules.values())
        wanted = [s for s, r in self.rules.items() if not is_excluded(r, known_bases)]
        if MARKET_PROXY_SYMBOL not in wanted:
            wanted.append(MARKET_PROXY_SYMBOL)
        self._candles, self._candles_day = {}, today
        for i, symbol in enumerate(wanted):
            if i:
                self.sleep(UNIVERSE_FETCH_PAUSE_SECONDS)
            got = self._call("daily_candles", symbol, CANDLE_COUNT)
            if got.ok and got.value:                       # 실패한 종목만 뺀다
                self._candles[symbol] = got.value
        symbols = select_universe(self.rules, self._candles)
        return symbols, f"감시 종목 {len(symbols)}개 < 최소 {UNIVERSE_MIN_COUNT}개"

    # ---- 3·4단계: 계좌와 대조 ---------------------------------------------------------
    def _sync_account(self) -> None:
        res = self._call("balances")
        if res.ok:
            self.balances = dict(res.value)
            self._clear_halt(ACCOUNT_FETCH)
            self._clear_halt(MAINTENANCE)                  # 계좌 조회가 되면 점검은 끝난 것으로 본다
        elif res.kind != "fatal":
            self._set_halt(ACCOUNT_FETCH, f"계좌 조회 실패: {res.error}")

    def base_asset(self, symbol: str) -> str:
        rule = self.rules.get(symbol)
        if rule is not None:
            return rule.base
        return symbol[:-len(QUOTE_ASSET)] if symbol.endswith(QUOTE_ASSET) else symbol

    def _reconcile(self) -> None:
        if self.balances is None:                          # 모르면 판정하지 않는다(기존 상태 유지)
            return
        if self.positions:                                 # 냉시작: 허용 오차(수량 단위)를 알아야 한다
            self._ensure_rules(self._now)                  # 실패하면 오차 0(fail-closed)
        short = []
        for symbol, pos in self.positions.items():
            rule = self.rules.get(symbol)
            free, locked = self.balances.get(self.base_asset(symbol), (0.0, 0.0))
            held = _dec(free) + _dec(locked)
            need = _dec(pos["qty"]) - (_dec(rule.step_size) if rule else Decimal(0))
            if held < need:
                short.append(f"{symbol} 장부 {pos['qty']} / 거래소 {float(held)}")
        if short:
            self._set_halt(RECON_MISMATCH, "계좌·장부 불일치: " + ", ".join(short))
        else:
            self._clear_halt(RECON_MISMATCH)
        managed = {self.base_asset(s) for s in self.positions}
        self.unmanaged_assets = sorted(
            asset for asset, (free, locked) in self.balances.items()
            if free + locked > 0 and asset != QUOTE_ASSET and asset not in managed)

    # ---- 6단계: 캔들과 평가액 기록 ------------------------------------------------------
    def _prepare_candles(self, now: datetime) -> None:
        today = _day(now)
        wanted = set(self.positions) | {MARKET_PROXY_SYMBOL}
        if self.universe.get("date") == today:
            wanted |= set(self.universe.get("symbols", []))
        ordered = sorted(wanted)
        if ordered != self._feed_symbols:
            self.feed.set_symbols(ordered)
            self._feed_symbols = ordered
        row = self.risk_state.get("pending_equity_row")
        if not row:
            return
        proxy = self.candles(MARKET_PROXY_SYMBOL, now)
        if not proxy:
            return
        closes = {c.get("date"): c["close"] for c in proxy}
        if row["day"] in closes:
            btc = closes[row["day"]]
        elif row["day"] < proxy[0].get("date", ""):
            btc = None                                     # 캔들 창보다 오래된 날: BTC 없이 기록
        else:
            return                                         # 그날 봉이 아직 없다: 다음 루프에 다시
        self._append_equity_row(row["day"], row["equity"], btc)
        del self.risk_state["pending_equity_row"]          # 기록이 저장된 뒤에만 지운다

    def _append_equity_row(self, day: str, equity: float, btc: float | None) -> None:
        self.equity_history = append_equity(self.equity_history, day, equity, btc)
        self.store.save("equity_history", self.equity_history)

    def candles(self, symbol: str, now: datetime) -> list[dict] | None:
        """그날의 일봉(REST, 하루 한 번) + 진행 중 봉을 피드 시세로 덮어쓴 사본. 받을 수 없으면 None."""
        today = _day(now)
        if self._candles_day != today:
            self._candles, self._candles_day = {}, today
        cached = self._candles.get(symbol)
        if cached is None:
            res = self._call("daily_candles", symbol, CANDLE_COUNT)
            if not res.ok or not res.value:
                return None
            cached = self._candles[symbol] = res.value
        out = [dict(c) for c in cached]
        quote = self.feed.quote(symbol, QUOTE_MAX_AGE_SECONDS)
        if quote is None or quote.day != today:
            return out
        live = {"date": today, "open": quote.open, "high": quote.high, "low": quote.low,
                "close": quote.price, "vol": quote.volume, "quote_vol": quote.quote_volume}
        if out[-1].get("date") == today:
            out[-1].update(live)
        elif out[-1].get("date", "") < today:              # REST에 오늘 봉이 없으면 붙인다(원본과 같다)
            out = (out + [live])[-CANDLE_COUNT:]
        return out

    # ---- 7단계: 평가액, 낙폭, 일일 손실 ------------------------------------------------
    def _evaluate_equity(self) -> None:
        rs = self.risk_state
        prices: dict[str, float] = {}
        missing: list[str] = []
        for symbol in self.positions:
            quote = self.feed.quote(symbol, QUOTE_MAX_AGE_SECONDS)
            if quote is None:
                missing.append(symbol)
            else:
                prices[symbol] = quote.price
        connected, error = self.feed.health()
        stale = []
        if not connected:
            stale.append(f"시세 연결 끊김 {error}".strip())
        if self.feed.quote(MARKET_PROXY_SYMBOL, QUOTE_MAX_AGE_SECONDS) is None:
            stale.append(f"{MARKET_PROXY_SYMBOL} 시세 없음")
        if missing:
            stale.append("보유 종목 시세 없음: " + ", ".join(missing))
        if stale:
            self._set_halt(FEED_STALE, "; ".join(stale))
        else:
            self._clear_halt(FEED_STALE)
        self.cash, _ = account_value(rs, self.positions, {s: 0.0 for s in self.positions})
        if missing:                                        # 평가액 모름: 일일 손실·낙폭은 판정하지 않는다
            return
        self.cash, self.equity = account_value(rs, self.positions, prices)
        rs["last_equity"] = self.equity
        if rs.get("day_open_equity") is None:
            rs["day_open_equity"] = self.equity
        open_equity = float(rs["day_open_equity"])
        if open_equity > 0:
            daily_return = (self.equity - open_equity) / open_equity
            if daily_return <= -DAILY_KILL_SWITCH_PCT:
                self._set_halt(DAILY_LOSS, f"일일 손실 {daily_return:.2%}")
        peak, self.drawdown, self.size_multiplier, halted = drawdown_controls(
            self.equity, rs.get("peak_equity"))
        rs["peak_equity"] = peak
        if halted:
            self._set_halt(DRAWDOWN, f"고점 대비 낙폭 {self.drawdown:.2%}")
        else:
            self._clear_halt(DRAWDOWN)

    # ---- 중단, 이벤트, 저장 ------------------------------------------------------------
    def _set_halt(self, code: str, detail: str) -> None:
        new = not self.halts.is_set(code)
        self.halts.set(code, detail, self._now)
        if new:
            self._event("RISK", detail)

    def _clear_halt(self, code: str) -> None:
        if self.halts.is_set(code):
            self.halts.clear(code)
            self._event("SYSTEM", f"중단 해제: {code}")

    def _event(self, kind: str, reason: str, symbol: str = "", **fields) -> None:
        event = {"type": kind, "symbol": symbol, "reason": reason,
                 "timestamp": self._now.isoformat(timespec="seconds"), **fields}
        self.events = record_event(self.events, event, self._now)
        self._loop_events.append(event)

    def _account_halt_time(self, now: datetime) -> None:
        prev, self._prev_loop_at = self._prev_loop_at, now
        if prev is None:
            return
        seconds = min(max((now - prev).total_seconds(), 0.0), 2 * POLL_INTERVAL_SECONDS)
        for halt in self.halts.active():
            self.metrics.add_seconds(f"halt:{halt['code']}", seconds, now)

    def _save_state(self) -> None:
        s = self.store
        s.save("positions", self.positions)
        s.save("intents", self.intents)
        s.save("equity_history", self.equity_history)     # risk_state의 대기 줄보다 먼저
        s.save("risk_state", self.risk_state)
        s.save("universe", self.universe)
        s.save("events", self.events)
        s.save("halts", self.halts.to_dict())

    # ---- 조회 ---------------------------------------------------------------------
    def entry_allowed(self, now: datetime) -> bool:
        return (not self.halts.active()
                and self.universe.get("date") == _day(now) and bool(self.universe.get("symbols"))
                and bool(self.rules)
                and not in_entry_blackout(now)
                and len(self.positions) < MAX_POSITIONS)

    def snapshot(self, now: datetime) -> dict:
        halts = []
        for halt in self.halts.active():
            item = dict(halt)
            since = self.halts.since(halt["code"])
            if halt["code"] == RECON_MISMATCH and (now - since).total_seconds() > RECON_ESCALATE_SECONDS:
                item["needs_manual"] = True
            halts.append(item)
        return {
            "mode": self.settings.mode, "now": now.isoformat(), "halts": halts,
            "equity": self.equity, "cash": self.cash,
            "universe_date": self.universe.get("date"), "universe": list(self.universe.get("symbols", [])),
            "positions": copy.deepcopy(self.positions), "unmanaged_assets": list(self.unmanaged_assets),
            "entry_allowed": self.entry_allowed(now), "signal_progress": self.entries.progress(),
        }
