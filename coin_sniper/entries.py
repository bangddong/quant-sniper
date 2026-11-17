"""진입 (Task 12): 신호 평가, 연속 확인, 위험 기준 수량, 주문 절차로 매수, 보호 손절 주문.

엔진 8단계(_manage_entries)가 entry_allowed(now)일 때만 부른다. 평가액·현금·계좌 잔고 중 하나라도 이번 루프에
모르면 아무것도 평가하지 않는다(fail-closed). 평가하지 않은 루프에서는 연속 확인 횟수를 처음부터 다시 센다:
신호는 "평가한 루프" 3회 연속으로 유지돼야 한다. 확인 횟수는 메모리에만 있다(키가 튜플이라 저장하지 않는다).

체결 반영(포지션·체결 기록)은 Executor._buy_journal이 저널로 한다. 여기서는 주문을 낼지만 정한다.
"""
from __future__ import annotations

from datetime import datetime

from coin_sniper import config
from coin_sniper.config import MARKET_PROXY_SYMBOL, MAX_POSITIONS, QUOTE_ASSET, QUOTE_MAX_AGE_SECONDS, SIGNAL_CONFIRMATIONS
from coin_sniper.risk import account_value, position_open_risk, size_entry
from coin_sniper.strategy import confirm_persistent_signals, day_progress, evaluate_signals

SKIP_HELD = "이미 보유 중"
SKIP_INTENT = "미확정 주문 있음"
SKIP_SLOTS = "최대 보유 종목 수 도달"
SKIP_RULE = "종목 규칙 없음"
SKIP_QUOTE = "실시간 시세 없음"
SKIP_SIZE = "투자·위험 한도 또는 최소 주문 금액 미달"
RISK_LOW_USDT = "실제 USDT가 봇 현금보다 적음"


class EntryManager:
    """엔진의 상태와 도구를 그대로 쓴다(Executor와 같은 방식)."""

    def __init__(self, engine):
        self.e = engine
        self.confirmations: dict[tuple[str, str], int] = {}
        self.raw = 0                       # 이번 루프 신호 수
        self.confirmed = 0                 # 이번 루프 확인된 신호 수
        self.evaluated = False             # 이번 루프에 평가했는가

    # ---- 루프 경계 -------------------------------------------------------------------
    def begin_loop(self) -> None:
        self.raw = self.confirmed = 0
        self.evaluated = False

    def end_loop(self) -> None:
        """평가하지 않은 루프는 연속이 끊긴 것이다."""
        if not self.evaluated:
            self.confirmations = {}

    def progress(self) -> list[dict]:
        return [{"symbol": s, "strategy": st, "count": n}
                for (s, st), n in sorted(self.confirmations.items())]

    # ---- 8단계 ---------------------------------------------------------------------
    def run(self, now: datetime) -> None:
        e = self.e
        if e.equity is None or e.cash is None or e.balances is None:
            return                                         # 모르면 평가하지 않는다(end_loop가 초기화)
        self.evaluated = True
        candles_by_symbol = {}
        for symbol in e.universe.get("symbols", []):
            candles = e.candles(symbol, now)
            if candles:
                candles_by_symbol[symbol] = candles
        market = e.candles(MARKET_PROXY_SYMBOL, now) or []
        signals = evaluate_signals(candles_by_symbol, market, day_progress(now))
        confirmed, self.confirmations = confirm_persistent_signals(
            signals, self.confirmations, SIGNAL_CONFIRMATIONS)
        self.raw, self.confirmed = len(signals), len(confirmed)
        free_usdt = float(e.balances.get(QUOTE_ASSET, (0.0, 0.0))[0])
        for signal in confirmed:                           # score 오름차순(evaluate_signals 순서)
            spent, stop = self._try_entry(signal, free_usdt, now)
            free_usdt -= spent
            if stop:
                break

    def _skip(self, symbol: str, reason: str) -> tuple[float, bool]:
        self.e._event("SKIP", reason, symbol)
        return 0.0, False

    def _try_entry(self, signal: dict, free_usdt: float, now: datetime) -> tuple[float, bool]:
        """(이번 매수로 쓴 USDT, 남은 신호 평가를 멈출지)."""
        e = self.e
        symbol = signal["symbol"]
        if symbol in e.positions:
            return self._skip(symbol, SKIP_HELD)
        if any(i["symbol"] == symbol for i in e.intents.values()):
            return self._skip(symbol, SKIP_INTENT)
        open_buys = sum(1 for i in e.intents.values() if i["side"] == "BUY")
        if len(e.positions) + open_buys >= MAX_POSITIONS:
            return self._skip(symbol, SKIP_SLOTS)
        rule = e.rules.get(symbol)
        if rule is None or not rule.trading:
            return self._skip(symbol, SKIP_RULE)
        quote = e.feed.quote(symbol, QUOTE_MAX_AGE_SECONDS)
        if quote is None:
            return self._skip(symbol, SKIP_QUOTE)
        bot_cash, _ = account_value(e.risk_state, e.positions, {s: 0.0 for s in e.positions})
        if free_usdt < bot_cash:
            e._event("RISK", RISK_LOW_USDT)
        cash = min(bot_cash, free_usdt)
        open_risk = sum(position_open_risk(p) for p in e.positions.values())
        qty = size_entry(e.equity, cash, quote.price, signal["stop_loss_pct"], open_risk, rule,
                         e.size_multiplier)
        if qty == 0:
            return self._skip(symbol, SKIP_SIZE)
        extra = {"stop_loss_pct": signal["stop_loss_pct"], "trailing_gap_pct": signal["trailing_gap_pct"],
                 "strategy": signal["strategy"]}
        outcome = e.executor.execute_market(symbol, "BUY", qty, "entry", signal["strategy"], now,
                                            ref_price=quote.price, extra=extra)
        for key in [k for k in self.confirmations if k[0] == symbol]:
            del self.confirmations[key]                    # 시도한 종목은 처음부터 다시 확인한다
        if outcome.status in ("filled", "partial"):
            pos = e.positions.get(symbol)
            if pos is None:                                # 반영되지 않았다: 모르는 상태로 보고 멈춘다
                return 0.0, True
            self._protect(symbol, rule, pos, quote.price, now)
            return float(pos["cost"]), False
        if outcome.status == "rejected":
            return 0.0, False
        return 0.0, True                                   # unconfirmed 등: 이번 루프 진입 중단

    def _protect(self, symbol: str, rule, pos: dict, price: float, now: datetime) -> None:
        """매수 직후 거래소 보호 손절 주문. 실패는 등록 경로가 RISK로 남기고 다음 루프 청산 단계가 다시 건다.
        팔 수 없는 잔량이면 걸지 않는다(다음 청산 단계의 잔량 정리가 처리한다)."""
        ex = self.e.executor
        if not config.PROTECTIVE_STOPS_ENABLED or ex._is_dust(rule, float(pos["qty"]), price):
            return
        ex._ensure_protective(symbol, rule, float(pos["stop_price"]), now)
