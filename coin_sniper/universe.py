"""일별 감시 종목 선정 (순수 함수)."""
from __future__ import annotations

from coin_sniper.config import (
    EXCLUDED_BASES, LEVERAGED_SUFFIXES, MIN_HISTORY_DAYS, MIN_PREV_DAY_QUOTE_VOLUME,
    QUOTE_ASSET, UNIVERSE_TOP_N,
)
from coin_sniper.models import SymbolRules


def is_excluded(rules: SymbolRules) -> bool:
    base = rules.base
    if base in EXCLUDED_BASES:
        return True
    if any(base.endswith(s) and len(base) > len(s) for s in LEVERAGED_SUFFIXES):
        return True
    return rules.quote != QUOTE_ASSET or not rules.trading


def select_universe(rules: dict[str, SymbolRules],
                    candles_by_symbol: dict[str, list[dict]]) -> list[str]:
    """전일(완료된 마지막 봉 = candles[-2]) 거래대금 기준 상위 종목."""
    ranked: list[tuple[float, str]] = []
    for symbol, rule in rules.items():
        if is_excluded(rule):
            continue
        candles = candles_by_symbol.get(symbol)
        if not candles or len(candles) < max(2, MIN_HISTORY_DAYS):
            continue
        prev_quote_vol = candles[-2]["quote_vol"]
        if prev_quote_vol < MIN_PREV_DAY_QUOTE_VOLUME:
            continue
        ranked.append((prev_quote_vol, symbol))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [symbol for _, symbol in ranked[:UNIVERSE_TOP_N]]
