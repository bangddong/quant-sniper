"""거래소·시세 계층의 프로토콜. 엔진은 이 인터페이스에만 의존한다."""
from __future__ import annotations

from typing import Iterable, Protocol

from coin_sniper.models import ApiResult, Quote


class Exchange(Protocol):
    def server_time(self) -> ApiResult: ...                                   # value: int(ms)
    def symbol_rules(self) -> ApiResult: ...                                  # value: dict[str, SymbolRules]
    def daily_candles(self, symbol: str, limit: int) -> ApiResult: ...        # value: list[dict], 과거→현재, 마지막은 진행 중인 봉
    def balances(self) -> ApiResult: ...                                      # value: dict[str, tuple[float, float]] (free, locked)
    def market_buy(self, symbol: str, qty: float, client_id: str) -> ApiResult: ...    # value: OrderState
    def market_sell(self, symbol: str, qty: float, client_id: str) -> ApiResult: ...
    def place_stop(self, symbol: str, qty: float, stop_price: float, limit_price: float, client_id: str) -> ApiResult: ...
    def cancel_order(self, symbol: str, client_id: str) -> ApiResult: ...
    def get_order(self, symbol: str, client_id: str) -> ApiResult: ...        # 모르는 주문이면 ok=True, value.exists=False
    def open_orders(self, symbol: str | None = None) -> ApiResult: ...        # value: list[OrderState]


class Feed(Protocol):
    def start(self) -> None: ...
    def set_symbols(self, symbols: Iterable[str]) -> None: ...
    def quote(self, symbol: str, max_age: float = 15) -> Quote | None: ...
    def health(self) -> tuple[bool, str]: ...
