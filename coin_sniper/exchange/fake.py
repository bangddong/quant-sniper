"""테스트용 가짜 거래소와 시세 피드. 실제 어댑터와 같은 프로토콜을 구현하며 결과가 결정적이다.

- 시장가 주문은 set_price로 정한 가격에 즉시 체결된다.
- 실패는 예외가 아니라 ApiResult(ok=False)로 돌려준다(실제 어댑터와 같다).
- 공개 Exchange 메서드 호출은 모두 calls에 (이름, 인자)로 기록된다(실패한 호출 포함).
"""
from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Callable, Iterable

from coin_sniper import config
from coin_sniper.models import ApiResult, OrderState, Quote, SymbolRules
from coin_sniper.risk import floor_to_step

_SERVER_TIME_MS = 1_794_000_000_000


def _d(value: float) -> Decimal:
    return Decimal(repr(float(value)))


def _fail(kind: str, error: str) -> ApiResult:
    return ApiResult(ok=False, value=None, kind=kind, error=error)


def _snap(state: OrderState) -> OrderState:
    """호출자가 내부 상태를 바꾸거나 나중 변화를 보지 않도록 복사본을 돌려준다."""
    return replace(state, fees=dict(state.fees))


def _is_multiple(value: float, unit: float) -> bool:
    return _d(value) % _d(unit) == 0


@dataclass
class _Order:
    state: OrderState
    qty: Decimal
    stop_price: float = 0.0
    limit_price: float = 0.0
    order_type: str = "STOP_LOSS_LIMIT"


def _api(fn: Callable) -> Callable:
    """호출 기록 + fail_next / drop_response_next 주입."""
    name = fn.__name__
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        bound = sig.bind(self, *args, **kwargs)
        bound.apply_defaults()
        params = dict(bound.arguments)
        params.pop("self")
        self.calls.append((name, params))
        queue = self._failures.get(name)
        if queue:
            return queue.pop(0)
        result = fn(self, *args, **kwargs)
        if self._drops.get(name, 0) > 0:
            self._drops[name] -= 1
            return _fail("transient", "response lost")
        return result

    return wrapper


class FakeExchange:
    def __init__(self, rules: dict[str, SymbolRules], balances: dict[str, float],
                 fee_rate: float = 0.001):
        self._rules = dict(rules)
        self._assets: dict[str, list[Decimal]] = {a: [_d(v), Decimal(0)] for a, v in balances.items()}
        self._fee_rate = _d(fee_rate)
        self._prices: dict[str, float] = {}
        self._candles: dict[str, list[dict]] = {}
        self._orders: dict[str, _Order] = {}
        self._failures: dict[str, list[ApiResult]] = {}
        self._drops: dict[str, int] = {}
        self._fill_ratio = 1.0
        self._fee_asset: tuple[str, Decimal] | None = None   # 수수료를 낼 제3 자산과 그 USDT 가격
        self._hidden = 0                   # 남은 "조회 지연" get_order 횟수
        self.calls: list[tuple[str, dict]] = []
        self.tags: dict[str, list[str]] = {}   # asset_tags() 응답

    # ---- 테스트 조작 ------------------------------------------------------------
    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = float(price)

    def set_candles(self, symbol: str, candles: list[dict]) -> None:
        self._candles[symbol] = list(candles)

    def fail_next(self, method: str, result: ApiResult, times: int = 1) -> None:
        self._failures.setdefault(method, []).extend([result] * times)

    def set_fill_ratio(self, ratio: float) -> None:
        self._fill_ratio = float(ratio)

    def set_fee_asset(self, asset: str | None, price: float = 0.0) -> None:
        """시장가 체결 수수료를 제3 자산(예: BNB)으로 낸다: 수수료 가치(체결 금액 × 수수료율)를 price로 나눈 수량을
        그 자산 잔고에서 뺀다. 기초 자산·USDT는 수수료 없이 오간다. None이면 원래대로."""
        self._fee_asset = None if asset is None else (asset, _d(price))

    def drop_response_next(self, method: str) -> None:
        self._drops[method] = self._drops.get(method, 0) + 1

    def set_trading(self, symbol: str, trading: bool) -> None:
        self._rules[symbol] = replace(self._rules[symbol], trading=trading)

    def hide_order_for(self, n_calls: int) -> None:
        """다음 n번의 get_order는 (어떤 주문이든) exists=False로 답한다: 거래소 조회 지연 흉내."""
        self._hidden = int(n_calls)

    def trigger_stops(self, fill_ratio: float = 1.0) -> None:
        """발동 조건(가격 ≤ 발동가)인 NEW 손절 주문을 체결한다: STOP_LOSS_LIMIT은 지정가, STOP_LOSS는 현재가. fill_ratio < 1이면
        floor_to_step(수량 × 비율)만 체결되고 PARTIALLY_FILLED로 남는다(나머지는 잠긴 채, 취소 가능)."""
        for order in self._orders.values():
            state = order.state
            if state.status != "NEW":
                continue
            price = self._prices.get(state.symbol)
            if price is None or price > order.stop_price:
                continue
            rule = self._rules[state.symbol]
            executed = order.qty if fill_ratio >= 1.0 else _d(
                floor_to_step(float(order.qty) * fill_ratio, rule.step_size))
            fill_price = price if order.order_type == "STOP_LOSS" else order.limit_price
            quote_qty = executed * _d(fill_price)
            fee = quote_qty * self._fee_rate
            self._account(rule.base)[1] -= executed
            self._account(rule.quote)[0] += quote_qty - fee
            state.status = "FILLED" if executed == order.qty else "PARTIALLY_FILLED"
            state.executed_qty = float(executed)
            state.quote_qty = float(quote_qty)
            state.fees = {rule.quote: float(fee)}

    # ---- 내부 -------------------------------------------------------------------
    def _account(self, asset: str) -> list[Decimal]:
        return self._assets.setdefault(asset, [Decimal(0), Decimal(0)])

    def _check_order(self, symbol: str, qty: float, client_id: str, notional_price: float | None):
        """공통 검증. 실패하면 ApiResult, 통과하면 규칙(SymbolRules)을 돌려준다."""
        rule = self._rules.get(symbol)
        if rule is None:
            return _fail("permanent", f"HTTP 400 code -1121: Invalid symbol {symbol}")
        if not rule.trading:
            return _fail("permanent", f"HTTP 400 code -1013: Market is closed for {symbol}")
        if client_id in self._orders:
            return _fail("permanent", f"HTTP 400 code -2010: Duplicate client order id {client_id}")
        if not _is_multiple(qty, rule.step_size):
            return _fail("permanent", f"HTTP 400 code -1013: Filter failure: LOT_SIZE ({qty})")
        if notional_price is None:
            return _fail("permanent", f"HTTP 400 code -1013: no price available for {symbol}")
        if _d(qty) * _d(notional_price) < _d(rule.min_notional):
            return _fail("permanent", "HTTP 400 code -1013: Filter failure: NOTIONAL")
        return rule

    def _market(self, symbol: str, side: str, qty: float, client_id: str) -> ApiResult:
        rule = self._check_order(symbol, qty, client_id, self._prices.get(symbol))
        if isinstance(rule, ApiResult):
            return rule
        price = _d(self._prices[symbol])
        want = _d(qty)
        if side == "BUY":
            if self._account(rule.quote)[0] < want * price:
                return _fail("permanent", "HTTP 400 code -2010: Account has insufficient balance")
        elif self._account(rule.base)[0] < want:
            return _fail("permanent", "HTTP 400 code -2010: Account has insufficient balance")
        ratio, self._fill_ratio = self._fill_ratio, 1.0
        executed = want if ratio == 1.0 else _d(floor_to_step(float(want) * ratio, rule.step_size))
        quote_qty = executed * price
        fees: dict[str, float] = {}
        if executed > 0 and self._fee_asset is not None:
            asset, asset_price = self._fee_asset
            fee = quote_qty * self._fee_rate / asset_price
            self._account(asset)[0] -= fee
            if side == "BUY":
                self._account(rule.quote)[0] -= quote_qty
                self._account(rule.base)[0] += executed
            else:
                self._account(rule.base)[0] -= executed
                self._account(rule.quote)[0] += quote_qty
            fees = {asset: float(fee)}
        elif executed > 0:
            if side == "BUY":
                fee = executed * self._fee_rate
                self._account(rule.quote)[0] -= quote_qty
                self._account(rule.base)[0] += executed - fee
                fees = {rule.base: float(fee)}
            else:
                fee = quote_qty * self._fee_rate
                self._account(rule.base)[0] -= executed
                self._account(rule.quote)[0] += quote_qty - fee
                fees = {rule.quote: float(fee)}
        state = OrderState(client_id=client_id, symbol=symbol, side=side,
                           status="FILLED" if executed == want else "EXPIRED",
                           executed_qty=float(executed), quote_qty=float(quote_qty), fees=fees)
        self._orders[client_id] = _Order(state=state, qty=want)
        return ApiResult(ok=True, value=_snap(state))

    # ---- Exchange 프로토콜 --------------------------------------------------------
    @_api
    def server_time(self) -> ApiResult:
        return ApiResult(ok=True, value=_SERVER_TIME_MS)

    @_api
    def symbol_rules(self) -> ApiResult:
        return ApiResult(ok=True, value=dict(self._rules))

    @_api
    def asset_tags(self) -> ApiResult:
        return ApiResult(ok=True, value={s: list(t) for s, t in self.tags.items()})

    @_api
    def daily_candles(self, symbol: str, limit: int) -> ApiResult:
        candles = self._candles.get(symbol, [])
        return ApiResult(ok=True, value=[dict(c) for c in candles[-limit:]] if limit > 0 else [])

    @_api
    def balances(self) -> ApiResult:
        return ApiResult(ok=True, value={a: (float(v[0]), float(v[1])) for a, v in self._assets.items()})

    @_api
    def market_buy(self, symbol: str, qty: float, client_id: str) -> ApiResult:
        return self._market(symbol, "BUY", qty, client_id)

    @_api
    def market_sell(self, symbol: str, qty: float, client_id: str) -> ApiResult:
        return self._market(symbol, "SELL", qty, client_id)

    @_api
    def place_stop(self, symbol: str, qty: float, stop_price: float, limit_price: float,
                   client_id: str) -> ApiResult:
        rule = self._check_order(symbol, qty, client_id, limit_price)
        if isinstance(rule, ApiResult):
            return rule
        if not _is_multiple(stop_price, rule.tick_size) or not _is_multiple(limit_price, rule.tick_size):
            return _fail("permanent", "HTTP 400 code -1013: Filter failure: PRICE_FILTER")
        want = _d(qty)
        account = self._account(rule.base)
        if account[0] < want:
            return _fail("permanent", "HTTP 400 code -2010: Account has insufficient balance")
        account[0] -= want
        account[1] += want
        state = OrderState(client_id=client_id, symbol=symbol, side="SELL", status="NEW")
        self._orders[client_id] = _Order(state=state, qty=want, stop_price=float(stop_price),
                                         limit_price=float(limit_price), order_type=config.PROTECTIVE_STOP_TYPE)
        return ApiResult(ok=True, value=_snap(state))

    @_api
    def cancel_order(self, symbol: str, client_id: str) -> ApiResult:
        order = self._orders.get(client_id)
        if order is None or order.state.symbol != symbol:
            return _fail("permanent", "HTTP 400 code -2011: Unknown order sent.")
        if order.state.status not in ("NEW", "PARTIALLY_FILLED"):
            return _fail("permanent", f"HTTP 400 code -2011: Order is {order.state.status}, cannot cancel")
        rule = self._rules[symbol]
        account = self._account(rule.base)
        rest = order.qty - _d(order.state.executed_qty)
        account[1] -= rest
        account[0] += rest
        order.state.status = "CANCELED"
        # 실제 어댑터처럼: 취소(DELETE) 응답에는 fills가 없어 수수료가 비어 있다. get_order는 실제 수수료를 준다.
        return ApiResult(ok=True, value=replace(order.state, fees={}))

    @_api
    def get_order(self, symbol: str, client_id: str) -> ApiResult:
        order = self._orders.get(client_id)
        hidden = self._hidden > 0
        if hidden:
            self._hidden -= 1
        if hidden or order is None or order.state.symbol != symbol:
            return ApiResult(ok=True, value=OrderState(client_id=client_id, symbol=symbol, side="",
                                                       status="UNKNOWN", exists=False))
        return ApiResult(ok=True, value=_snap(order.state))

    @_api
    def open_orders(self, symbol: str | None = None) -> ApiResult:
        return ApiResult(ok=True, value=[_snap(o.state) for o in self._orders.values()
                                         if not o.state.is_final
                                         and (symbol is None or o.state.symbol == symbol)])


class FakeFeed:
    def __init__(self):
        self.now = 1_800_000_000.0       # 피드 자체 시계(고정)
        self.symbols: list[str] = []
        self.started = 0
        self._quotes: dict[str, Quote] = {}
        self._health: tuple[bool, str] = (True, "")

    def set_quote(self, symbol, price, open=None, high=None, low=None, volume=0.0,
                  day="2026-11-17", age=0.0) -> None:
        self._quotes[symbol] = Quote(
            symbol=symbol, price=price,
            open=price if open is None else open,
            high=price if high is None else high,
            low=price if low is None else low,
            volume=volume, quote_volume=volume * price, day=day,
            received_at=self.now - age)

    def set_health(self, connected: bool, error: str = "") -> None:
        self._health = (connected, error)

    def start(self) -> None:
        self.started += 1

    def set_symbols(self, symbols: Iterable[str]) -> None:
        self.symbols = list(symbols)

    def quote(self, symbol: str, max_age: float = 15) -> Quote | None:
        q = self._quotes.get(symbol)
        if q is None or self.now - q.received_at > max_age:
            return None
        return q

    def health(self) -> tuple[bool, str]:
        return self._health
