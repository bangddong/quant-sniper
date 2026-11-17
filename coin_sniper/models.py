"""거래소 계층과 전략이 공유하는 타입."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApiResult:
    ok: bool
    value: Any = None
    kind: str = "ok"          # "ok" | "transient" | "permanent" | "fatal"
    error: str = ""
    retry_after: float = 0.0


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    base: str
    quote: str
    trading: bool
    step_size: float
    tick_size: float
    min_notional: float


@dataclass
class Quote:
    symbol: str
    price: float
    open: float
    high: float
    low: float
    volume: float
    quote_volume: float
    day: str                  # 이 봉의 UTC 날짜 "YYYY-MM-DD"
    received_at: float        # epoch 초


@dataclass
class OrderState:
    client_id: str
    symbol: str
    side: str                 # "BUY" | "SELL"
    status: str               # "NEW"|"PARTIALLY_FILLED"|"FILLED"|"CANCELED"|"EXPIRED"|"REJECTED"
    exists: bool = True       # 거래소가 모르는 주문이면 False
    executed_qty: float = 0.0
    quote_qty: float = 0.0    # 체결 금액 합계(USDT)
    fees: dict[str, float] = field(default_factory=dict)   # 자산 → 수수료 수량

    @property
    def avg_price(self) -> float:
        return self.quote_qty / self.executed_qty if self.executed_qty else 0.0

    @property
    def is_final(self) -> bool:
        return self.status not in ("NEW", "PARTIALLY_FILLED")
