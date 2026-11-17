"""리스크 계산 순수 함수. 시각·I/O 없이 인자만으로 결과가 정해진다."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal

from . import config
from .models import SymbolRules


def _d(value: float) -> Decimal:
    # 부동소수 잡음(0.1+0.2 등)을 피하려고 표기 문자열로 변환한다.
    return Decimal(str(value))


def _floor(value: float, unit: float) -> float:
    if not unit > 0:
        raise ValueError("unit must be positive")
    unit_d = _d(unit)
    return float((_d(value) // unit_d) * unit_d)


def floor_to_step(qty: float, step: float) -> float:
    return _floor(qty, step)


def floor_to_tick(price: float, tick: float) -> float:
    return _floor(price, tick)


def account_value(risk_state: dict, positions: dict, prices: dict[str, float]) -> tuple[float, float]:
    """가상 계좌의 (현금, 평가자산). 가격이 없는 보유 종목은 KeyError."""
    cash = float(risk_state["starting_capital"]) + float(risk_state["realized_pnl"])
    cash -= sum(float(p["cost"]) for p in positions.values())
    equity = cash + sum(float(p["qty"]) * float(prices[sym]) for sym, p in positions.items())
    return cash, equity


def position_open_risk(pos: dict) -> float:
    """현재 스탑에서 청산될 때의 손실액. 스탑이 매수가 이상이면 0."""
    loss = (float(pos["buy_price"]) - float(pos["stop_price"])) * float(pos["qty"])
    return max(0.0, loss)


def drawdown_controls(equity: float, peak: float | None) -> tuple[float, float, float, bool]:
    """(고점, 낙폭, 신규 주문 배율, 중단 여부)."""
    equity = max(float(equity or 0), 0.0)
    peak = max(float(peak if peak is not None else equity), equity)
    drawdown = (peak - equity) / peak if peak else 0.0
    halted = drawdown >= config.EQUITY_HALT_DRAWDOWN_PCT
    multiplier = 0.5 if drawdown >= config.EQUITY_SLOWDOWN_DRAWDOWN_PCT else 1.0
    return peak, drawdown, multiplier, halted


def size_entry(equity: float, cash: float, entry_price: float, stop_loss_pct: float,
               open_risk_total: float, rules: SymbolRules, size_multiplier: float = 1.0) -> float:
    """종목당 한도·거래당 위험·열린 위험 잔여·현금을 모두 지키는 수량(스텝 내림)."""
    inputs = (equity, cash, entry_price, stop_loss_pct, size_multiplier)
    if not all(math.isfinite(v) and v > 0 for v in inputs) or not math.isfinite(open_risk_total):
        return 0.0
    equity_d, mult = _d(equity), _d(size_multiplier)
    price, risk_per_unit = _d(entry_price), _d(entry_price) * _d(stop_loss_pct)
    remaining = equity_d * _d(config.MAX_OPEN_RISK_PCT) - _d(open_risk_total)
    if remaining <= 0:
        return 0.0
    risk_budget = min(equity_d * _d(config.MAX_RISK_PER_TRADE_PCT) * mult, remaining)
    qty = min(
        equity_d * _d(config.MAX_ALLOC_PER_SYMBOL) * mult / price,
        risk_budget / risk_per_unit,
        _d(cash) / price,
    )
    floored = _floor(float(qty), rules.step_size)
    if _d(floored) * price < _d(rules.min_notional):
        return 0.0
    return floored


def update_stop(pos: dict, high_water: float) -> tuple[float, str]:
    """고점을 갱신하고 절대 낮아지지 않는 스탑을 계산해 pos에 반영한다."""
    buy = _d(pos["buy_price"])
    highest = max(_d(pos["highest_price"]), _d(high_water))
    pos["highest_price"] = float(highest)
    one = Decimal(1)
    if highest >= buy * (one + _d(config.TRAIL_TRIGGER_PCT)):
        level = highest * (one - _d(pos["trailing_gap_pct"]))
        pos["trailing_active"] = True
        reason = "고점 추적 트레일링 스탑"
    elif highest >= buy * (one + _d(config.PROTECT_TRIGGER_PCT)):
        level = buy * (one + _d(config.PROTECT_LOCK_PCT))
        reason = "수익 보호 스탑"
    else:
        level = buy * (one - _d(pos["stop_loss_pct"]))
        reason = "동적 손절"
    stop = max(float(pos["stop_price"]), float(level))
    pos["stop_price"] = stop
    return stop, reason


def time_stop_due(pos: dict, now: datetime) -> bool:
    entry = datetime.fromisoformat(pos["entry_time"])
    return now >= entry + timedelta(hours=config.MAX_HOLD_HOURS)
