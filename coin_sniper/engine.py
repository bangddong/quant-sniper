"""코인 봇 엔진 루프: 계좌, 날짜 변경, 감시 종목, 대조, 중단.

원칙: 확인하지 못한 것은 "모름"이다. 모르면 신규 주문을 하지 않는다(fail-closed).
시각은 항상 now 인자(UTC, timezone-aware)로 받는다. 하루 경계는 UTC 0시.
청산(Task 11)과 진입(Task 12)은 _resolve_intents / _manage_exits / _manage_entries 자리에 끼워 넣는다.
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
from coin_sniper.halts import (
    ACCOUNT_FETCH, DAILY_LOSS, DRAWDOWN, FATAL, FEED_STALE, RATE_LIMIT, RECON_MISMATCH, UNIVERSE,
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
        self._universe_tried_at: datetime | None = None
        self._pending_equity: tuple[str, float] | None = None   # (전날, 전날 마지막 평가액)
        self._prev_loop_at: datetime | None = None
        self._now: datetime | None = None
        self._loop_events: list[dict] = []

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
        ok = self._guarded(self._steps, now)
        if ok:
            self.risk_state["last_loop_at"] = now.isoformat()
        ok = self._guarded(self._save_state) and ok
        self.metrics.record_loop(ok, now)
        self._guarded(lambda: self.store.save("metrics", self.metrics.to_dict()))
        return LoopReport(now=now, ok=ok, equity=self.equity, cash=self.cash,
                          entry_allowed=self.entry_allowed(now), halts=self.halts.active(),
                          events=list(self._loop_events))

    def _steps(self, now: datetime) -> None:
        self.balances = None
        self.equity = None
        self._account_halt_time(now)
        until = self.risk_state.get("rate_limit_until")
        if until and now < datetime.fromisoformat(until):
            return                                         # 대기 중: 거래소를 전혀 부르지 않는다
        self.risk_state["rate_limit_until"] = None
        self._clear_halt(RATE_LIMIT)
        try:
            self._resolve_intents(now)                     # 1 (Task 11)
            self._roll_day(now)                            # 2
            self._sync_account()                           # 3
            self._reconcile()                              # 4
            self._prepare_candles(now)                     # 5
            self._manage_exits(now)                        # 6 (Task 11)
            self._evaluate_equity()                        # 7
            self._manage_entries(now)                      # 8 (Task 12)
        except _RateLimited:
            return

    def _guarded(self, fn: Callable, *args) -> bool:
        try:
            fn(*args)
            return True
        except Exception as exc:                           # KeyboardInterrupt/SystemExit는 잡지 않는다
            self._event("ERROR", f"{type(exc).__name__}: {exc}")
            return False

    # ---- 이후 작업이 채우는 자리 -----------------------------------------------------
    def _resolve_intents(self, now: datetime) -> None:
        """1단계: 진행 중 주문 의도 해소 (Task 11)."""

    def _manage_exits(self, now: datetime) -> None:
        """6단계: 청산과 보호 주문 관리 (Task 11)."""

    def _manage_entries(self, now: datetime) -> None:
        """8단계: 진입 (Task 12)."""

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

    # ---- 2단계: 날짜 변경과 감시 종목 ---------------------------------------------------
    def _roll_day(self, now: datetime) -> None:
        today = _day(now)
        rs = self.risk_state
        if rs.get("day") != today:
            if rs.get("day") and rs.get("last_equity") is not None:
                self._pending_equity = (rs["day"], float(rs["last_equity"]))
            rs["day"] = today
            rs["day_open_equity"] = None                   # 오늘 첫 평가액으로 7단계에서 정한다
            self._clear_halt(DAILY_LOSS)
            self._universe_tried_at = None
        throttled = (self._universe_tried_at is not None
                     and (now - self._universe_tried_at).total_seconds() < UNIVERSE_RETRY_SECONDS)
        if self.universe.get("date") != today or (not self.universe.get("symbols") and not throttled):
            self._refresh_universe(now, today)
        elif not self.rules and not throttled:             # 재시작: 오늘 목록은 있고 규칙만 없다
            self._universe_tried_at = now
            res = self._call("symbol_rules")
            if res.ok:
                self.rules = dict(res.value)
                self._clear_halt(UNIVERSE)
            else:
                self._set_halt(UNIVERSE, f"종목 규칙 조회 실패: {res.error}")

    def _refresh_universe(self, now: datetime, today: str) -> None:
        self._universe_tried_at = now
        symbols: list[str] = []
        res = self._call("symbol_rules")
        if res.ok:
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
                if got.ok and got.value:                   # 실패한 종목만 뺀다
                    self._candles[symbol] = got.value
            symbols = select_universe(self.rules, self._candles)
            detail = f"감시 종목 {len(symbols)}개 < 최소 {UNIVERSE_MIN_COUNT}개"
        else:
            detail = f"종목 규칙 조회 실패: {res.error}"
        if len(symbols) >= UNIVERSE_MIN_COUNT:
            self.universe = {"date": today, "symbols": symbols}
            self._clear_halt(UNIVERSE)
        else:                                              # 이전 목록으로 대체하지 않는다
            self.universe = {"date": today, "symbols": []}
            self._set_halt(UNIVERSE, detail)

    # ---- 3·4단계: 계좌와 대조 ---------------------------------------------------------
    def _sync_account(self) -> None:
        res = self._call("balances")
        if res.ok:
            self.balances = dict(res.value)
            self._clear_halt(ACCOUNT_FETCH)
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

    # ---- 5단계: 캔들 ---------------------------------------------------------------
    def _prepare_candles(self, now: datetime) -> None:
        today = _day(now)
        wanted = set(self.positions) | {MARKET_PROXY_SYMBOL}
        if self.universe.get("date") == today:
            wanted |= set(self.universe.get("symbols", []))
        ordered = sorted(wanted)
        if ordered != self._feed_symbols:
            self.feed.set_symbols(ordered)
            self._feed_symbols = ordered
        if self._pending_equity is not None:
            proxy = self.candles(MARKET_PROXY_SYMBOL, now)
            if proxy and len(proxy) >= 2:
                day, equity = self._pending_equity
                self.equity_history = append_equity(self.equity_history, day, equity, proxy[-2]["close"])
                self._pending_equity = None

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
        if quote is not None and quote.day == today and out[-1].get("date") == today:
            out[-1].update(open=quote.open, high=quote.high, low=quote.low, close=quote.price,
                           vol=quote.volume, quote_vol=quote.quote_volume)
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

    def _event(self, kind: str, reason: str, symbol: str = "") -> None:
        event = {"type": kind, "symbol": symbol, "reason": reason,
                 "timestamp": self._now.isoformat(timespec="seconds")}
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
        s.save("risk_state", self.risk_state)
        s.save("universe", self.universe)
        s.save("events", self.events)
        s.save("equity_history", self.equity_history)
        s.save("halts", self.halts.to_dict())

    # ---- 조회 ---------------------------------------------------------------------
    def entry_allowed(self, now: datetime) -> bool:
        return (not self.halts.active()
                and self.universe.get("date") == _day(now) and bool(self.universe.get("symbols"))
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
            "entry_allowed": self.entry_allowed(now),
        }
