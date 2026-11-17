"""주문 실행: 시장가 주문 절차, 체결 반영, 청산, 거래소 보호 손절 주문, 주문 의도 복구 (Task 11).

주문 절차(스펙 7.1): 의도 저장 → 전송 → get_order로 확인 → 장부 반영 → 의도 삭제.
모든 거래소 호출은 엔진의 _call을 거친다(호출 한도·확정 오류 판정이 한곳에 있다).

장부 반영은 저널 방식이다. 반영 결과(체결 기록, 반영 뒤 포지션, 반영 뒤 실현 손익, 잔량)를 먼저 계산해
의도에 "journal"로 저장하고, 그다음 trades → dust → positions → risk_state 순으로 값을 덮어쓴 뒤 의도를 지운다.
중간에 프로세스가 죽으면 재시작 1단계(resolve_intents)가 같은 저널을 다시 쓴다. 덮어쓰기라 몇 번 써도 결과가 같고,
체결 기록은 client_id로 중복을 막으므로 한 체결이 두 번 반영되지 않는다.

매수(Task 12, entries.py): execute_market(symbol, "BUY", ...)로 같은 절차를 타고 _buy_journal이 저널을 만든다.
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from coin_sniper import config
from coin_sniper.config import (
    INTENT_NOT_FOUND_GRACE_SECONDS, MAX_HOLD_HOURS, ORDER_CONFIRM_TIMEOUT_SECONDS, QUOTE_ASSET, QUOTE_MAX_AGE_SECONDS,
    STOP_LIMIT_OFFSET_PCT, STOP_REPLACE_MIN_MOVE_PCT,
)
from coin_sniper.halts import LOOP_ERROR, MAINTENANCE, ORDER_UNCONFIRMED, PROTECTIVE_CANCEL
from coin_sniper.ledger import add_intent, new_client_id
from coin_sniper.models import ApiResult, OrderState, SymbolRules
from coin_sniper.risk import floor_to_step, floor_to_tick, time_stop_due, update_stop

TIME_STOP_REASON = f"타임스탑 ({MAX_HOLD_HOURS}시간 경과)"
EXCHANGE_STOP_REASON = "거래소 손절"
EXCHANGE_STOP_PARTIAL_REASON = "거래소 손절(부분)"
BUY_DROPPED_REASON = "매수 주문 미확인 폐기: 거래소 잔고 확인 필요"
_SIDE_KO = {"BUY": "매수", "SELL": "매도"}


def _dec(value: float) -> Decimal:
    return Decimal(repr(float(value)))


@dataclass
class Outcome:
    """execute_market 결과.
    status: "filled"(요청 수량 − 1스텝 이상 체결) | "partial"(일부 체결) | "rejected"(체결 0, 거래소가 거절·만료)
            | "absent"(거래소에 주문 없음) | "unconfirmed"(확정 못 함: 의도는 남고 ORDER_UNCONFIRMED)."""
    status: str
    client_id: str
    state: OrderState | None = None
    error: str = ""


class Executor:
    """엔진의 상태(positions, intents, risk_state, rules)와 도구(_call, sleep, store, 이벤트·중단)를 그대로 쓴다.
    passthrough: 잡지 않고 올려 보낼 예외(엔진의 _RateLimited)."""

    def __init__(self, engine, passthrough: tuple[type[BaseException], ...] = ()):
        self.e = engine
        self._passthrough = passthrough

    # ==== 공개 진입점: 시장가 주문 ======================================================
    def execute_market(self, symbol: str, side: str, qty: float, kind: str, reason: str,
                       now: datetime, ref_price: float | None = None, extra: dict | None = None) -> Outcome:
        """시장가 주문 하나를 끝까지 처리한다. qty는 호출자가 이미 step으로 내린 값이어야 한다.
        kind: "entry" | "exit" (client_id 접두어). ref_price: 판단에 쓴 시세(슬리피지 기준).
        extra: 체결 반영에 필요한 값(매수의 손절 설정 등). 의도에 저장되어 재시작 후에도 쓰인다.
        체결은 확인된 것만 장부에 반영된다(매도는 _sell_journal, 매수는 _buy_journal)."""
        e = self.e
        cid = new_client_id(e.settings.mode, kind)
        add_intent(e.intents, cid, symbol, side, qty, kind, reason, now)
        e.intents[cid].update(ref_price=ref_price, extra=dict(extra or {}))
        e.store.save("intents", e.intents)                 # 보내기 전에 저장(write-ahead)
        e.metrics.record_order("sent", now)
        send = e._call("market_buy" if side == "BUY" else "market_sell", symbol, qty, cid)
        if not send.ok and send.kind == "transient":       # 실패한 그 시점에 건다(다음 계좌 조회 성공 때 해제)
            e._set_halt(MAINTENANCE, f"거래소 주문 실패(일시 오류): {symbol} {_SIDE_KO[side]} ({send.error})")
        rejected = not send.ok and send.kind == "permanent"
        state = self._confirm(symbol, cid, rejected)       # 보낸 결과와 상관없이 항상 조회로 확인
        return self._settle(cid, state, send, now)

    def _confirm(self, symbol: str, cid: str, accept_absent: bool) -> OrderState | None:
        """is_final이 될 때까지 1초 간격으로 조회한다. "없음"은 명시적 거절 뒤에만 바로 받아들이고,
        그 밖에는 조회 지연일 수 있으므로 계속 조회한다. 끝까지 없으면 마지막 "없음" 상태를 돌려준다."""
        last = None
        for attempt in range(max(1, int(ORDER_CONFIRM_TIMEOUT_SECONDS))):
            if attempt:
                self.e.sleep(1)
            res = self.e._call("get_order", symbol, cid)
            if not res.ok:
                continue
            if res.value.exists and res.value.is_final:
                return res.value
            if not res.value.exists:
                if accept_absent:
                    return res.value
                last = res.value
        return last

    def _settle(self, cid: str, state: OrderState | None, send: ApiResult | None,
                now: datetime) -> Outcome:
        """확인 결과 분류. send가 None이면 재시작 후 의도 해소(전송 결과를 모른다).
        체결 정보(수량·금액·수수료)는 항상 get_order 한 곳에서만 가져온다(전송 응답은 쓰지 않는다)."""
        e = self.e
        intent = e.intents[cid]
        symbol, side = intent["symbol"], intent["side"]
        if state is None:
            return self._unknown(cid, now)
        send_failed = send is not None and not send.ok
        if not state.exists:
            if send_failed and send.kind == "permanent":   # 거래소가 명시적으로 거절: 바로 지운다
                self._drop_intent(cid)
                e.metrics.record_order("rejected", now)
                e._event("RISK", f"{_SIDE_KO[side]} 거절: {send.error}", symbol)
                return Outcome("rejected", cid, state, send.error)
            age = (now - datetime.fromisoformat(intent["created_at"])).total_seconds()
            if age < INTENT_NOT_FOUND_GRACE_SECONDS:       # 조회 지연일 수 있다: 모름
                return self._unknown(cid, now)
            self._drop_intent(cid)                         # 유예가 지나도 없다: 주문된 적 없음
            if side == "BUY":                              # 늦게 체결되면 장부 모르는 코인이 생긴다
                e._event("RISK", BUY_DROPPED_REASON, symbol)
            return Outcome("absent", cid, state)
        if state.executed_qty <= 0:
            self._drop_intent(cid)
            e.metrics.record_order("rejected", now)
            error = send.error if send_failed else f"주문 상태 {state.status}"
            e._event("RISK", f"{_SIDE_KO[side]} 거절: {error}", symbol)
            return Outcome("rejected", cid, state, error)
        rule = e.rules.get(symbol)
        if rule is None:                                   # 수량 단위·최소 금액을 모르면 반영하지 않는다
            return self._unknown(cid, now, "종목 규칙 미확보")
        full = _dec(state.executed_qty) >= _dec(intent["qty"]) - _dec(rule.step_size)
        self._apply(cid, state, now)
        e.metrics.record_order("filled" if full else "partial", now)
        return Outcome("filled" if full else "partial", cid, state)

    def _unknown(self, cid: str, now: datetime, why: str = "") -> Outcome:
        """결과를 모른다: 의도를 남기고 ORDER_UNCONFIRMED. 미확인 지표는 의도당 한 번만 센다."""
        e = self.e
        intent = e.intents[cid]
        if not intent.get("unconfirmed_logged"):
            intent["unconfirmed_logged"] = True
            e.store.save("intents", e.intents)
            e.metrics.record_order("unconfirmed", now)
        detail = f"체결 미확인: {intent['symbol']} {_SIDE_KO[intent['side']]} {cid}"
        e._set_halt(ORDER_UNCONFIRMED, f"{detail} ({why})" if why else detail)
        return Outcome("unconfirmed", cid)

    # ==== 1단계: 의도 해소 =============================================================
    def resolve_intents(self, now: datetime) -> None:
        e = self.e
        if not e.intents:
            e._clear_halt(ORDER_UNCONFIRMED)
            return
        e._ensure_rules(now)                               # 재시작 직후엔 규칙이 비어 있다
        no_rules = set()
        for cid in list(e.intents):
            intent = e.intents.get(cid)
            if intent is None:
                continue
            if "journal" in intent:                        # 반영 도중 죽었다: 같은 값을 다시 쓴다
                self._commit(cid)
                continue
            if intent["symbol"] not in e.rules:            # 규칙 없이는 반영하지 않는다: 남겨 둔다
                no_rules.add(intent["symbol"])
                continue
            res = e._call("get_order", intent["symbol"], cid)
            if not res.ok or (res.value.exists and not res.value.is_final):
                self._unknown(cid, now)                    # 모름: 남겨 두고 다음 루프에 다시
                continue
            self._settle(cid, res.value, None, now)
        if e.intents:
            pending = ", ".join(sorted({i["symbol"] for i in e.intents.values()}))
            why = f" (종목 규칙 미확보: {', '.join(sorted(no_rules))})" if no_rules else ""
            e._set_halt(ORDER_UNCONFIRMED, f"체결 미확인 주문: {pending}{why}")
        else:
            e._clear_halt(ORDER_UNCONFIRMED)

    # ==== 체결 반영 ===================================================================
    def _apply(self, cid: str, state: OrderState, now: datetime) -> None:
        e = self.e
        intent = e.intents[cid]
        if self._trade_recorded(cid):                      # 이미 반영된 체결
            self._drop_intent(cid)
            return
        rule = e.rules.get(intent["symbol"])
        if rule is None:                                   # 명시적 방어: 단위를 모르는 반영은 하지 않는다
            raise RuntimeError(f"종목 규칙 없이 체결 반영 불가: {intent['symbol']}")
        if intent["side"] == "SELL":
            if intent["symbol"] not in e.positions:        # 체결을 지어내지 않는다
                self._drop_intent(cid)
                e._event("RISK", f"매도 체결 반영 불가: 장부에 포지션 없음 ({cid})", intent["symbol"])
                return
            journal = self._sell_journal(intent, state, rule, now)
        else:
            journal = self._buy_journal(intent, state, rule, now)
        intent["journal"] = journal
        e.store.save("intents", e.intents)                 # 저널이 먼저 디스크에 있어야 한다
        self._commit(cid)

    def _buy_journal(self, intent: dict, state: OrderState, rule: SymbolRules, now: datetime) -> dict:
        """매수 체결 반영 저널. 장부는 바꾸지 않는다(_commit이 쓴다).
        수량은 실제 받은 수량(체결 − 코인으로 낸 수수료, 내림 없음), 원가는 체결 금액 + USDT 수수료.
        1스텝 미만·최소 금액 미만의 작은 체결도 그대로 기록한다(다음 청산 단계의 잔량 정리가 닫는다)."""
        symbol, extra = intent["symbol"], intent.get("extra") or {}
        received = float(max(_dec(state.executed_qty) - _dec(state.fees.get(rule.base, 0.0)), Decimal(0)))
        quote_qty = float(state.quote_qty)
        cost = quote_qty + float(state.fees.get(QUOTE_ASSET, 0.0))
        buy_price = state.avg_price
        stop_loss_pct = float(extra["stop_loss_pct"])
        position = {
            "symbol": symbol, "qty": received, "buy_price": buy_price, "cost": cost,
            "stop_loss_pct": stop_loss_pct, "trailing_gap_pct": float(extra["trailing_gap_pct"]),
            "highest_price": buy_price, "stop_price": buy_price * (1 - stop_loss_pct), "trailing_active": False,
            "entry_time": now.astimezone(timezone.utc).isoformat(), "strategy": extra.get("strategy"),
            "protective": None,
        }
        ref = intent.get("ref_price")
        trade = {
            "time": now.isoformat(), "type": "BUY", "symbol": symbol, "qty": received, "price": buy_price,
            "quote_qty": quote_qty, "fees": dict(state.fees), "strategy": extra.get("strategy"),
            "reason": intent["reason"], "client_id": intent["client_id"],
            "slippage_pct": (buy_price - ref) / ref * 100 if ref else None,
        }
        return {"trade": trade, "position": position, "dust": None,
                "realized_pnl": float(self.e.risk_state.get("realized_pnl", 0.0))}

    def _sell_journal(self, intent: dict, state: OrderState, rule: SymbolRules, now: datetime) -> dict:
        e = self.e
        symbol = intent["symbol"]
        pos = copy.deepcopy(e.positions[symbol])
        step = _dec(rule.step_size)
        sold, held = _dec(state.executed_qty), _dec(pos["qty"])
        quote_qty = float(state.quote_qty)
        proceeds = quote_qty - float(state.fees.get(QUOTE_ASSET, 0.0))
        cost = float(pos["cost"])
        cost_part = cost if sold >= held - step else cost * float(sold / held)
        gross = quote_qty - float(pos["buy_price"]) * float(sold)
        net = proceeds - cost_part
        ref, avg = intent.get("ref_price"), state.avg_price
        slippage = (ref - avg) / ref * 100 if ref else None
        pos["qty"] = float(max(held - sold, Decimal(0)))
        pos["cost"] = cost - cost_part
        if intent["kind"] == "protective":
            pos["protective"] = None                       # 그 보호 주문은 끝났다
        writeoff, dust = 0.0, None
        if self._is_dust(rule, pos["qty"], avg or float(pos["buy_price"])):
            writeoff = pos["cost"]                         # 팔 수 없는 잔량의 원가는 손실로 정리
            if pos["qty"] > 0:
                dust = {"symbol": symbol, "qty": pos["qty"], "time": now.isoformat(), "client_id": intent["client_id"]}
            pos = None
        net -= writeoff
        base = cost_part + writeoff
        trade = {
            "time": now.isoformat(), "type": "SELL", "symbol": symbol, "qty": float(sold),
            "price": avg, "quote_qty": quote_qty, "fees": dict(state.fees),
            "strategy": e.positions[symbol].get("strategy"), "reason": intent["reason"],
            "client_id": intent["client_id"], "entry_price": float(e.positions[symbol]["buy_price"]),
            "gross_pnl": gross, "net_pnl": net, "net_return_pct": net / base * 100 if base else 0.0,
            "slippage_pct": slippage, "counted": True, "dust_writeoff": writeoff,
        }
        return {"trade": trade, "position": pos, "dust": dust,
                "realized_pnl": float(e.risk_state.get("realized_pnl", 0.0)) + net}

    @staticmethod
    def _is_dust(rule: SymbolRules, qty: float, price: float) -> bool:
        return qty <= 0 or floor_to_step(qty, rule.step_size) == 0 or qty * price < rule.min_notional

    def _close_dust(self, symbol: str, now: datetime) -> None:
        """팔 수 없는 포지션(1스텝 미만 또는 최소 주문 금액 미만)을 저널로 닫는다: 잔량 기록, 남은 원가는 손실,
        통계에서 빠지는 체결 기록(counted False), RISK 이벤트 하나."""
        e = self.e
        pos = e.positions[symbol]
        cid = f"dust-{uuid.uuid4().hex[:12]}"
        qty, cost = float(pos["qty"]), float(pos["cost"])
        add_intent(e.intents, cid, symbol, "SELL", qty, "dust", "잔량 정리", now)
        trade = {
            "time": now.isoformat(), "type": "SELL", "symbol": symbol, "qty": 0, "price": None,
            "quote_qty": 0.0, "fees": {}, "strategy": pos.get("strategy"), "reason": "잔량 정리",
            "client_id": cid, "entry_price": float(pos["buy_price"]), "gross_pnl": 0.0, "net_pnl": -cost,
            "net_return_pct": -100.0 if cost else 0.0, "slippage_pct": None, "counted": False,
            "dust_writeoff": cost,
        }
        dust = {"symbol": symbol, "qty": qty, "time": now.isoformat(), "client_id": cid} if qty > 0 else None
        e.intents[cid].update(ref_price=None, extra={}, journal={
            "trade": trade, "position": None, "dust": dust,
            "realized_pnl": float(e.risk_state.get("realized_pnl", 0.0)) - cost})
        e.store.save("intents", e.intents)
        self._commit(cid)
        e._event("RISK", "잔량 정리: 최소 주문 미만 포지션 종료", symbol)

    def _commit(self, cid: str) -> None:
        """저널을 장부에 쓴다. 몇 번 불려도 결과가 같다."""
        e = self.e
        intent = e.intents[cid]
        journal, symbol = intent["journal"], intent["symbol"]
        trades = e.store.load("trades", [])
        new = not any(t.get("client_id") == cid for t in trades)
        if new:
            trades.append(journal["trade"])
            e.store.save("trades", trades)
        if journal.get("dust"):
            dust = e.store.load("dust", [])
            if not any(d.get("client_id") == cid for d in dust):
                dust.append(journal["dust"])
                e.store.save("dust", dust)
        if journal["position"] is None:
            e.positions.pop(symbol, None)
        else:
            e.positions[symbol] = copy.deepcopy(journal["position"])
        e.store.save("positions", e.positions)
        e.risk_state["realized_pnl"] = journal["realized_pnl"]
        e.store.save("risk_state", e.risk_state)
        self._drop_intent(cid)
        trade = journal["trade"]
        if new and trade["type"] == "SELL" and trade.get("counted", True):
            e._event("SELL", trade["reason"], symbol, qty=trade["qty"], price=trade["price"],
                     net_return_pct=trade["net_return_pct"])
        elif new and trade["type"] == "BUY":
            e._event("BUY", trade["reason"], symbol, qty=trade["qty"], price=trade["price"])

    def _apply_external(self, symbol: str, state: OrderState, reason: str, now: datetime) -> None:
        """거래소 보호 주문의 체결을 매도로 반영한다(시장가 주문 없음). 같은 의도·저널 경로를 탄다."""
        e = self.e
        cid = state.client_id
        if cid in e.intents or self._trade_recorded(cid):
            return
        add_intent(e.intents, cid, symbol, "SELL", state.executed_qty, "protective", reason, now)
        e.intents[cid].update(ref_price=None, extra={})
        self._apply(cid, state, now)

    def _trade_recorded(self, cid: str) -> bool:
        return any(t.get("client_id") == cid for t in self.e.store.load("trades", []))

    def _drop_intent(self, cid: str) -> None:
        self.e.intents.pop(cid, None)
        self.e.store.save("intents", self.e.intents)

    # ==== 5단계: 청산과 보호 주문 =======================================================
    def manage_exits(self, now: datetime) -> None:
        """어떤 중단 상태에서도 돈다. balances·equity·캔들은 믿지 않는다(positions, rules, feed만)."""
        e = self.e
        busy = {i["symbol"] for i in e.intents.values()}  # 결과를 모르는 주문이 있으면 그 종목은 손대지 않는다
        for symbol in sorted(e.positions):
            if symbol not in e.positions or symbol in busy:
                continue
            try:
                self._manage_position(symbol, now)
            except self._passthrough:
                raise
            except Exception as exc:                       # 한 종목의 오류가 다른 종목 청산을 막지 않는다
                e._loop_failed = True
                e._event("ERROR", f"exits {symbol}: {type(exc).__name__}: {exc}", symbol)
                e._set_halt(LOOP_ERROR, f"루프 단계 오류(exits {symbol}): {type(exc).__name__}")
        self._derive_cancel_halt()
        e.store.save("positions", e.positions)

    def _derive_cancel_halt(self) -> None:
        """PROTECTIVE_CANCEL은 장부 상태에서 정한다: 취소 실패 표시나 최종 상태를 모르는 "canceled" 기록이
        하나라도 남아 있으면 건다. 건너뛴 포지션도 기록이 남아 있으므로 풀리지 않는다(확인돼야 풀린다)."""
        e = self.e
        items = []
        for symbol in sorted(e.positions):
            prot = e.positions[symbol].get("protective") or {}
            if prot.get("state") == "canceled":
                items.append(f"{symbol}: 취소된 보호 주문의 최종 상태 미확인")
            elif prot.get("cancel_failed"):
                items.append(f"{symbol}: 보호 주문 취소 실패 ({prot['cancel_failed']})")
        if items:
            e._set_halt(PROTECTIVE_CANCEL, "; ".join(items))
        else:
            e._clear_halt(PROTECTIVE_CANCEL)

    def _manage_position(self, symbol: str, now: datetime) -> None:
        e = self.e
        rule = e.rules.get(symbol)
        if rule is None:                                   # 수량·가격 단위를 모른다: 주문도 반영도 하지 않는다
            e._event("RISK", "종목 규칙 없음: 주문 보류", symbol)
            return
        enabled = config.PROTECTIVE_STOPS_ENABLED
        if enabled and e.positions[symbol].get("protective"):
            # 취소된 주문의 최종 상태를 모르면 남은 수량도 모른다: 팔지도, 새로 걸지도 않는다(중단은 유지)
            if not self._check_protective(symbol, now) or symbol not in e.positions:
                return
        pos = e.positions[symbol]
        quote = e.feed.quote(symbol, QUOTE_MAX_AGE_SECONDS)
        if quote is None:
            e._event("RISK", "실시간 시세 없음: 청산 판단 보류", symbol)
            return
        if self._is_dust(rule, float(pos["qty"]), quote.price):
            self._dust_exit(symbol, now)
            return
        pos["highest_price"] = max(float(pos["highest_price"]), quote.price)
        stop, reason = update_stop(pos, quote.price)
        if quote.price > stop:
            reason = TIME_STOP_REASON if time_stop_due(pos, now) else None
        if reason is not None:
            self._exit(symbol, rule, reason, quote.price, now)
            return
        prot = pos.get("protective")
        if prot and prot.pop("cancel_failed", None):       # 확인된 주문이 걸려 있고 취소할 이유도 사라졌다
            e.store.save("positions", e.positions)
        if enabled:
            self._ensure_protective(symbol, rule, stop, now)

    def _release_protective(self, symbol: str, now: datetime) -> str | None:
        """팔기 전에 보호 주문을 푼다. None이면 계속 진행, 아니면 _cancel_protective의 결과(진행 중단)."""
        e = self.e
        if not (config.PROTECTIVE_STOPS_ENABLED and e.positions[symbol].get("protective")):
            return None
        result = self._cancel_protective(symbol, now)      # 걸린 주문이 수량을 잠그므로 먼저 취소
        if result == "ok" and symbol in e.positions:
            return None
        return result

    def _exit(self, symbol: str, rule: SymbolRules, reason: str, price: float, now: datetime) -> None:
        e = self.e
        if self._release_protective(symbol, now) is not None:
            return
        qty = floor_to_step(e.positions[symbol]["qty"], rule.step_size)
        if self._is_dust(rule, float(e.positions[symbol]["qty"]), price):
            self._close_dust(symbol, now)                  # 보호 주문의 부분 체결 뒤 남은 게 잔량일 수 있다
        elif qty > 0:
            self.execute_market(symbol, "SELL", qty, "exit", reason, now, ref_price=price)

    def _dust_exit(self, symbol: str, now: datetime) -> None:
        if self._release_protective(symbol, now) is None:
            self._close_dust(symbol, now)

    def _check_protective(self, symbol: str, now: datetime) -> bool:
        """거래소의 보호 주문 상태를 장부에 맞춘다. 조회 실패면 False(이번 루프는 이 종목을 건너뛴다).
        체결은 항상 저널로 먼저 반영하고, 그다음에 protective를 비운다."""
        e = self.e
        prot = e.positions[symbol]["protective"]
        if prot.get("state") == "canceled":                # 취소는 됐고 체결 확인이 남았다: 다시 취소하지 않는다
            return self._settle_canceled(symbol, now)
        res = e._call("get_order", symbol, prot["client_id"])
        if not res.ok:                                     # 모름 ≠ 없음
            e._event("RISK", f"보호 주문 조회 실패: {res.error}", symbol)
            return False
        st = res.value
        if st.exists and st.status == "FILLED":
            self._apply_external(symbol, st, EXCHANGE_STOP_REASON, now)
            self._clear_protective(symbol, prot["client_id"])
            return True
        if st.exists and not st.is_final:
            if prot.get("state") != "open":
                prot["state"] = "open"
                e.store.save("positions", e.positions)
            return True
        if st.exists and st.executed_qty > 0:
            self._apply_external(symbol, st, EXCHANGE_STOP_PARTIAL_REASON, now)
        self._clear_protective(symbol, prot["client_id"])
        e._event("RISK", "보호 주문 없음: 재등록", symbol)
        return True

    def _cancel_protective(self, symbol: str, now: datetime) -> str:
        """"ok" | "failed"(취소 실패 표시 → PROTECTIVE_CANCEL) | "filled"(이미 체결: 거래소 손절로 반영)
        | "pending"(취소는 됐지만 최종 상태 미확인: "canceled"로 남기고 팔지 않는다 → PROTECTIVE_CANCEL).
        취소 응답의 체결 정보는 쓰지 않는다(수수료가 없다). 체결은 get_order로 확인한다."""
        e = self.e
        prot = e.positions[symbol]["protective"]
        cid = prot["client_id"]
        res = e._call("cancel_order", symbol, cid)
        if not res.ok:
            got = e._call("get_order", symbol, cid)
            if got.ok and got.value.exists and got.value.status == "FILLED":
                self._apply_external(symbol, got.value, EXCHANGE_STOP_REASON, now)
                self._clear_protective(symbol, cid)
                return "filled"
            prot["cancel_failed"] = res.error or "취소 실패"
            e.store.save("positions", e.positions)
            e._set_halt(PROTECTIVE_CANCEL, f"{symbol}: 보호 주문 취소 실패 ({res.error})")
            return "failed"
        prot.pop("cancel_failed", None)
        prot["state"] = "canceled"
        e.store.save("positions", e.positions)             # 죽어도 다음 루프가 체결 확인부터 한다
        return "ok" if self._settle_canceled(symbol, now) else "pending"

    def _settle_canceled(self, symbol: str, now: datetime) -> bool:
        """취소된 보호 주문의 최종 상태를 get_order(수수료 포함)로 확인해 체결분을 반영하고 기록을 지운다.
        조회 성공 + 존재 + 최종 상태일 때만 정산한다. 그 밖(실패·없음·최종 아님)은 기록을 그대로 두고 False.
        유예 후 삭제는 없다: 거래소가 취소를 확인했으니 주문은 있다."""
        e = self.e
        cid = e.positions[symbol]["protective"]["client_id"]
        res = e._call("get_order", symbol, cid)
        st = res.value if res.ok else None
        if st is None or not st.exists or not st.is_final:
            e._event("RISK", "취소된 보호 주문 최종 상태 미확인: 매도 보류", symbol)
            return False
        if st.executed_qty > 0:
            reason = EXCHANGE_STOP_REASON if st.status == "FILLED" else EXCHANGE_STOP_PARTIAL_REASON
            self._apply_external(symbol, st, reason, now)
        self._clear_protective(symbol, cid)
        return True

    def _clear_protective(self, symbol: str, cid: str) -> None:
        """저널 반영 뒤에만 부른다. 그 주문의 기록일 때만 지운다."""
        e = self.e
        pos = e.positions.get(symbol)
        if pos and pos.get("protective") and pos["protective"]["client_id"] == cid:
            pos["protective"] = None
            e.store.save("positions", e.positions)

    def _ensure_protective(self, symbol: str, rule: SymbolRules, stop: float, now: datetime) -> None:
        e = self.e
        stop_price = floor_to_tick(stop, rule.tick_size)
        limit_price = floor_to_tick(float(_dec(stop_price) * (1 - _dec(STOP_LIMIT_OFFSET_PCT))), rule.tick_size)
        prot = e.positions[symbol].get("protective")
        if prot:
            if prot.get("state") != "open" or stop_price < prot["stop_price"] * (1 + STOP_REPLACE_MIN_MOVE_PCT):
                return
            if self._cancel_protective(symbol, now) != "ok" or symbol not in e.positions:
                return
        qty = floor_to_step(e.positions[symbol]["qty"], rule.step_size)
        if qty > 0:
            self._place_protective(symbol, qty, stop_price, limit_price)

    def _place_protective(self, symbol: str, qty: float, stop_price: float, limit_price: float) -> None:
        e = self.e
        cid = new_client_id(e.settings.mode, "protective")
        pos = e.positions[symbol]
        pos["protective"] = {"client_id": cid, "stop_price": stop_price, "limit_price": limit_price,
                             "qty": qty, "state": "pending"}
        e.store.save("positions", e.positions)             # 보내기 전에 저장: 응답을 잃어도 찾을 수 있다
        res = e._call("place_stop", symbol, qty, stop_price, limit_price, cid)
        if res.ok:
            pos["protective"]["state"] = "open"
        elif res.kind == "permanent":
            pos["protective"] = None
            e._event("RISK", f"보호 주문 등록 실패: {res.error}", symbol)
        # transient/fatal: pending으로 남긴다. 다음 루프의 get_order가 정한다(있으면 open, 없으면 재등록)
        e.store.save("positions", e.positions)
