"""일별 감시 종목 선정 (순수 함수)."""
from __future__ import annotations

from coin_sniper.config import (
    EXCLUDED_BASES, LEVERAGED_SUFFIXES, MIN_HISTORY_DAYS, MIN_PREV_DAY_QUOTE_VOLUME,
    QUOTE_ASSET, UNIVERSE_TOP_N,
)
from coin_sniper.models import SymbolRules


def is_excluded(rules: SymbolRules, known_bases: frozenset[str] = frozenset()) -> bool:
    base = rules.base
    if base in EXCLUDED_BASES:
        return True
    # 레버리지 토큰: 접미사를 뗀 기초자산이 실제 상장 목록에 있을 때만 (JUP, SYRUP 보호)
    for suffix in LEVERAGED_SUFFIXES:
        if base.endswith(suffix):
            underlying = base[:-len(suffix)]
            if underlying and underlying in known_bases:
                return True
    return rules.quote != QUOTE_ASSET or not rules.trading


def select_universe(rules: dict[str, SymbolRules],
                    candles_by_symbol: dict[str, list[dict]],
                    excluded_symbols: frozenset[str] = frozenset()) -> list[str]:
    """전일(완료된 마지막 봉 = candles[-2]) 거래대금 기준 상위 종목. excluded_symbols = 분류 태그로 뺀 심볼."""
    known_bases = frozenset(r.base for r in rules.values())
    ranked: list[tuple[float, str]] = []
    for symbol, rule in rules.items():
        if symbol in excluded_symbols or is_excluded(rule, known_bases):
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
