"""주문 실행: 시장가 주문 절차, 체결 반영, 청산, 거래소 보호 손절 주문, 주문 의도 복구 (Task 11).

주문 절차(스펙 7.1): 의도 저장 → 전송 → get_order로 확인 → 장부 반영 → 의도 삭제.
모든 거래소 호출은 엔진의 _call을 거친다(호출 한도·확정 오류 판정이 한곳에 있다).

장부 반영은 저널 방식이다. 반영 결과(체결 기록, 반영 뒤 포지션, 반영 뒤 실현 손익, 잔량)를 먼저 계산해
의도에 "journal"로 저장하고, 그다음 trades → dust → positions → risk_state 순으로 값을 덮어쓴 뒤 의도를 지운다.
중간에 프로세스가 죽으면 재시작 1단계(resolve_intents)가 같은 저널을 다시 쓴다. 덮어쓰기라 몇 번 써도 결과가 같고,
체결 기록은 client_id로 중복을 막으므로 한 체결이 두 번 반영되지 않는다.

매수(Task 12): execute_market(symbol, "BUY", ...)을 부르고, _buy_journal을 채운다(그 메서드의 설명 참고).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from coin_sniper import config
from coin_sniper.config import (
    MAX_HOLD_HOURS, ORDER_CONFIRM_TIMEOUT_SECONDS, QUOTE_ASSET, QUOTE_MAX_AGE_SECONDS,
    STOP_LIMIT_OFFSET_PCT, STOP_REPLACE_MIN_MOVE_PCT,
)
from coin_sniper.halts import LOOP_ERROR, MAINTENANCE, ORDER_UNCONFIRMED, PROTECTIVE_CANCEL
from coin_sniper.ledger import add_intent, new_client_id
from coin_sniper.models import ApiResult, OrderState, SymbolRules
from coin_sniper.risk import floor_to_step, floor_to_tick, time_stop_due, update_stop

TIME_STOP_REASON = f"타임스탑 ({MAX_HOLD_HOURS}시간 경과)"
EXCHANGE_STOP_REASON = "거래소 손절"
EXCHANGE_STOP_PARTIAL_REASON = "거래소 손절(부분)"
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
        state = self._confirm(symbol, cid)                 # 보낸 결과와 상관없이 항상 조회로 확인
        return self._settle(cid, state, send, now)

    def _confirm(self, symbol: str, cid: str) -> OrderState | None:
        for attempt in range(max(1, int(ORDER_CONFIRM_TIMEOUT_SECONDS))):
            if attempt:
                self.e.sleep(1)
            res = self.e._call("get_order", symbol, cid)
            if res.ok and (not res.value.exists or res.value.is_final):
                return res.value
        return None

    def _settle(self, cid: str, state: OrderState | None, send: ApiResult | None,
                now: datetime) -> Outcome:
        """확인 결과 분류. send가 None이면 재시작 후 의도 해소(전송 결과를 모른다)."""
        e = self.e
        intent = e.intents[cid]
        symbol, side = intent["symbol"], intent["side"]
        if state is None:
            e.metrics.record_order("unconfirmed", now)
            e._set_halt(ORDER_UNCONFIRMED, f"체결 미확인: {symbol} {_SIDE_KO[side]} {cid}")
            return Outcome("unconfirmed", cid)
        send_failed = send is not None and not send.ok
        if not state.exists:
            self._drop_intent(cid)
            if send is None:                               # 거래소가 못 본 의도: 조용히 지운다
                return Outcome("absent", cid, state)
            e.metrics.record_order("rejected", now)
            error = send.error if send_failed else "거래소에 주문 없음"
            if send_failed and send.kind == "transient":
                e._set_halt(MAINTENANCE, f"거래소 주문 거부(일시 오류): {symbol} {_SIDE_KO[side]} ({error})")
            else:
                e._event("RISK", f"{_SIDE_KO[side]} 거절: {error}", symbol)
            return Outcome("absent", cid, state, error)
        if state.executed_qty <= 0:
            self._drop_intent(cid)
            e.metrics.record_order("rejected", now)
            error = send.error if send_failed else f"주문 상태 {state.status}"
            e._event("RISK", f"{_SIDE_KO[side]} 거절: {error}", symbol)
            return Outcome("rejected", cid, state, error)
        rule = e.rules.get(symbol)
        step = _dec(rule.step_size) if rule else Decimal(0)
        full = _dec(state.executed_qty) >= _dec(intent["qty"]) - step
        self._apply(cid, state, now)
        e.metrics.record_order("filled" if full else "partial", now)
        return Outcome("filled" if full else "partial", cid, state)

    # ==== 1단계: 의도 해소 =============================================================
    def resolve_intents(self, now: datetime) -> None:
        e = self.e
        for cid in list(e.intents):
            intent = e.intents.get(cid)
            if intent is None:
                continue
            if "journal" in intent:                        # 반영 도중 죽었다: 같은 값을 다시 쓴다
                self._commit(cid)
                continue
            res = e._call("get_order", intent["symbol"], cid)
            if not res.ok or (res.value.exists and not res.value.is_final):
                continue                                   # 모름: 남겨 두고 다음 루프에 다시
            self._settle(cid, res.value, None, now)
        if e.intents:
            pending = ", ".join(sorted({i["symbol"] for i in e.intents.values()}))
            e._set_halt(ORDER_UNCONFIRMED, f"체결 미확인 주문: {pending}")
        else:
            e._clear_halt(ORDER_UNCONFIRMED)

    # ==== 체결 반영 ===================================================================
    def _apply(self, cid: str, state: OrderState, now: datetime) -> None:
        e = self.e
        intent = e.intents[cid]
        if self._trade_recorded(cid):                      # 이미 반영된 체결
            self._drop_intent(cid)
            return
        if intent["side"] == "SELL":
            if intent["symbol"] not in e.positions:        # 체결을 지어내지 않는다
                self._drop_intent(cid)
                e._event("RISK", f"매도 체결 반영 불가: 장부에 포지션 없음 ({cid})", intent["symbol"])
                return
            journal = self._sell_journal(intent, state, now)
        else:
            journal = self._buy_journal(intent, state, now)
        intent["journal"] = journal
        e.store.save("intents", e.intents)                 # 저널이 먼저 디스크에 있어야 한다
        self._commit(cid)

    def _buy_journal(self, intent: dict, state: OrderState, now: datetime) -> dict:
        """매수 체결 반영 자리 (Task 12). 장부를 직접 바꾸지 말고 아래 저널을 돌려준다(_commit이 쓴다):
        {"trade": 체결 기록(type "BUY", client_id 필수), "position": 반영 뒤 이 종목 포지션 dict(없애려면 None),
         "realized_pnl": 반영 뒤 risk_state["realized_pnl"](매수는 그대로), "dust": None}.
        intent에는 symbol, side, qty, kind, reason, ref_price, extra(execute_market에 넘긴 값)가 있다."""
        raise NotImplementedError("매수 체결 반영은 Task 12에서 구현한다")

    def _sell_journal(self, intent: dict, state: OrderState, now: datetime) -> dict:
        e = self.e
        symbol = intent["symbol"]
        pos = copy.deepcopy(e.positions[symbol])
        rule = e.rules.get(symbol)
        step = _dec(rule.step_size) if rule else Decimal(0)
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
    def _is_dust(rule: SymbolRules | None, qty: float, price: float) -> bool:
        if qty <= 0:
            return True
        if rule is None:                                   # 규칙을 모르면 잔량 판정도 못 한다: 남겨 둔다
            return False
        return floor_to_step(qty, rule.step_size) == 0 or qty * price < rule.min_notional

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
        if new and trade["type"] == "SELL":
            e._event("SELL", trade["reason"], symbol, qty=trade["qty"], price=trade["price"],
                     net_return_pct=trade["net_return_pct"])

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
        cancel_failed = False
        for symbol in sorted(e.positions):
            if symbol not in e.positions or symbol in busy:
                continue
            try:
                cancel_failed = self._manage_position(symbol, now) or cancel_failed
            except self._passthrough:
                raise
            except Exception as exc:                       # 한 종목의 오류가 다른 종목 청산을 막지 않는다
                e._loop_failed = True
                e._event("ERROR", f"exits {symbol}: {type(exc).__name__}: {exc}", symbol)
                e._set_halt(LOOP_ERROR, f"루프 단계 오류(exits {symbol}): {type(exc).__name__}")
        if not cancel_failed:
            e._clear_halt(PROTECTIVE_CANCEL)
        e.store.save("positions", e.positions)

    def _manage_position(self, symbol: str, now: datetime) -> bool:
        """한 포지션 처리. 보호 주문 취소에 실패했으면 True."""
        e = self.e
        enabled = config.PROTECTIVE_STOPS_ENABLED
        if enabled and e.positions[symbol].get("protective"):
            if not self._check_protective(symbol, now) or symbol not in e.positions:
                return False
        pos = e.positions[symbol]
        quote = e.feed.quote(symbol, QUOTE_MAX_AGE_SECONDS)
        if quote is None:
            e._event("RISK", "실시간 시세 없음: 청산 판단 보류", symbol)
            return False
        pos["highest_price"] = max(float(pos["highest_price"]), quote.price)
        stop, reason = update_stop(pos, quote.price)
        if quote.price > stop:
            reason = TIME_STOP_REASON if time_stop_due(pos, now) else None
        if reason is None and not enabled:
            return False
        rule = e.rules.get(symbol)
        if rule is None:                                   # 수량·가격을 내릴 수 없다: 보내지 않는다
            e._event("RISK", "종목 규칙 없음: 주문 보류", symbol)
            return False
        if reason is not None:
            return self._exit(symbol, rule, reason, quote.price, now)
        return self._ensure_protective(symbol, rule, stop, now)

    def _exit(self, symbol: str, rule: SymbolRules, reason: str, price: float, now: datetime) -> bool:
        e = self.e
        if config.PROTECTIVE_STOPS_ENABLED and e.positions[symbol].get("protective"):
            result = self._cancel_protective(symbol, now)  # 걸린 주문이 수량을 잠그므로 먼저 취소
            if result == "failed":
                return True
            if symbol not in e.positions:
                return False
        qty = floor_to_step(e.positions[symbol]["qty"], rule.step_size)
        if qty > 0:
            self.execute_market(symbol, "SELL", qty, "exit", reason, now, ref_price=price)
        return False

    def _check_protective(self, symbol: str, now: datetime) -> bool:
        """거래소의 보호 주문 상태를 장부에 맞춘다. 조회 실패면 False(이번 루프는 이 종목을 건너뛴다)."""
        e = self.e
        prot = e.positions[symbol]["protective"]
        res = e._call("get_order", symbol, prot["client_id"])
        if not res.ok:                                     # 모름 ≠ 없음
            e._event("RISK", f"보호 주문 조회 실패: {res.error}", symbol)
            return False
        st = res.value
        if st.exists and st.status == "FILLED":
            self._apply_external(symbol, st, EXCHANGE_STOP_REASON, now)
            return True
        if st.exists and not st.is_final:
            if prot.get("state") != "open":
                prot["state"] = "open"
                e.store.save("positions", e.positions)
            return True
        if st.exists and st.executed_qty > 0:
            self._apply_external(symbol, st, EXCHANGE_STOP_PARTIAL_REASON, now)
        if symbol in e.positions:
            e.positions[symbol]["protective"] = None
            e.store.save("positions", e.positions)
        e._event("RISK", "보호 주문 없음: 재등록", symbol)
        return True

    def _cancel_protective(self, symbol: str, now: datetime) -> str:
        """"ok" | "failed"(PROTECTIVE_CANCEL 설정) | "filled"(이미 체결: 거래소 손절로 반영)."""
        e = self.e
        cid = e.positions[symbol]["protective"]["client_id"]
        res = e._call("cancel_order", symbol, cid)
        if not res.ok:
            got = e._call("get_order", symbol, cid)
            if got.ok and got.value.exists and got.value.status == "FILLED":
                self._apply_external(symbol, got.value, EXCHANGE_STOP_REASON, now)
                return "filled"
            e._set_halt(PROTECTIVE_CANCEL, f"보호 주문 취소 실패: {symbol} ({res.error})")
            return "failed"
        e.positions[symbol]["protective"] = None
        e.store.save("positions", e.positions)
        if res.value is not None and res.value.executed_qty > 0:
            self._apply_external(symbol, res.value, EXCHANGE_STOP_PARTIAL_REASON, now)
        return "ok"

    def _ensure_protective(self, symbol: str, rule: SymbolRules, stop: float, now: datetime) -> bool:
        e = self.e
        stop_price = floor_to_tick(stop, rule.tick_size)
        limit_price = floor_to_tick(float(_dec(stop_price) * (1 - _dec(STOP_LIMIT_OFFSET_PCT))), rule.tick_size)
        prot = e.positions[symbol].get("protective")
        if prot:
            if prot.get("state") != "open" or stop_price < prot["stop_price"] * (1 + STOP_REPLACE_MIN_MOVE_PCT):
                return False
            result = self._cancel_protective(symbol, now)
            if result == "failed":
                return True
            if result == "filled" or symbol not in e.positions:
                return False
        qty = floor_to_step(e.positions[symbol]["qty"], rule.step_size)
        if qty > 0:
            self._place_protective(symbol, qty, stop_price, limit_price)
        return False

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
