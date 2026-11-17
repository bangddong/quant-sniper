"""코인 일봉 진입 신호 (순수 함수). 마지막 캔들은 진행 중인 당일 봉이다."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from coin_sniper.config import (
    ATR_PERIOD, ATR_STOP_MULTIPLIER, ENTRY_BLACKOUT_MINUTES, MARKET_MA_PERIOD,
    MAX_ENTRY_DAY_RETURN_PCT, MAX_STOP_LOSS_PCT, MAX_TRAIL_GAP_PCT,
    MAX_UPPER_WICK_RATIO, MIN_STOP_LOSS_PCT, MIN_TRAIL_GAP_PCT, RSI_BUY_THRESHOLD,
    RSI_PERIOD, RSI_RECOVERY_THRESHOLD, SIGNAL_CONFIRMATIONS, VBO_K,
    VOLUME_CONFIRM_MULTIPLIER,
)

STRATEGY_RSI = "rsi_recovery"
STRATEGY_VBO = "vbo"

_MIN_DAY_PROGRESS = 0.05
_SECONDS_PER_DAY = 86400.0


def is_favorable_market(market_candles: list[dict]) -> bool:
    """시장 프록시가 장기 이동평균 위이고 장기선이 상승할 때만 진입한다."""
    if not market_candles or len(market_candles) < MARKET_MA_PERIOD + 1:
        return False
    closes = [c["close"] for c in market_candles]
    ma_now = sum(closes[-MARKET_MA_PERIOD:]) / MARKET_MA_PERIOD
    ma_prev = sum(closes[-MARKET_MA_PERIOD - 1:-1]) / MARKET_MA_PERIOD
    return closes[-1] > ma_now and ma_now > ma_prev


def calculate_rsi(closes: list[float], period: int = RSI_PERIOD) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    if len(gains) < period:
        return 50.0
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_atr(candles: list[dict], period: int = ATR_PERIOD) -> float:
    if len(candles) < period + 1:
        return candles[-1]["close"] * 0.03  # 기본 3% 변동성 가정
    tr_list = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_c = candles[i - 1]["close"]
        tr_list.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    if len(tr_list) < period:
        return candles[-1]["close"] * 0.03
    return sum(tr_list[-period:]) / period


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _risk_fields(atr: float, entry_price: float) -> tuple[float, float]:
    atr_pct = (atr / entry_price) if entry_price > 0 else 0.03
    stop = _clamp(atr_pct * ATR_STOP_MULTIPLIER, MIN_STOP_LOSS_PCT, MAX_STOP_LOSS_PCT)
    trail = _clamp(atr_pct, MIN_TRAIL_GAP_PCT, MAX_TRAIL_GAP_PCT)
    return round(stop, 4), round(trail, 4)


def evaluate_signals(candles_by_symbol: dict[str, list[dict]], market_candles: list[dict],
                     day_progress: float) -> list[dict]:
    """전 종목 일봉을 분석해 매수 신호를 score 오름차순(RSI 우선)으로 반환한다."""
    candidates: list[dict] = []
    if not is_favorable_market(market_candles):
        return candidates

    for symbol, candles in candles_by_symbol.items():
        if len(candles) < 25:
            continue

        closes = [c["close"] for c in candles]
        curr = candles[-1]
        prev = candles[-2]

        rsi = calculate_rsi(closes)
        prev_rsi = calculate_rsi(closes[:-1])
        ma20 = sum(closes[-20:]) / 20.0
        prev_ma20 = sum(closes[-21:-1]) / 20.0
        atr = calculate_atr(candles)

        prev_close = prev["close"]
        day_return_pct = (curr["close"] - prev_close) / prev_close if prev_close else 0.0
        candle_range = curr["high"] - curr["low"]
        if candle_range > 0:
            upper_wick_ratio = (curr["high"] - max(curr["open"], curr["close"])) / candle_range
        else:
            upper_wick_ratio = 0.0
        price_quality_ok = (
            day_return_pct <= MAX_ENTRY_DAY_RETURN_PCT
            and upper_wick_ratio <= MAX_UPPER_WICK_RATIO
        )

        is_rsi_signal = (
            prev_rsi <= RSI_BUY_THRESHOLD
            and rsi > RSI_RECOVERY_THRESHOLD
            and curr["close"] > curr["open"]
            and curr["close"] > prev["high"]
            and price_quality_ok
        )

        target_price = curr["open"] + (prev["high"] - prev["low"]) * VBO_K
        avg_volume20 = sum(c["vol"] for c in candles[-21:-1]) / 20.0
        expected_volume = avg_volume20 * max(_MIN_DAY_PROGRESS, min(1.0, day_progress))
        volume_confirmed = (
            expected_volume > 0
            and curr["vol"] >= expected_volume * VOLUME_CONFIRM_MULTIPLIER
        )
        is_vbo_signal = (
            curr["close"] >= target_price
            and curr["close"] > curr["open"]
            and curr["close"] > ma20
            and ma20 > prev_ma20
            and volume_confirmed
            and price_quality_ok
        )

        if is_rsi_signal:
            entry_p = curr["close"]
            stop, trail = _risk_fields(atr, entry_p)
            candidates.append({
                "symbol": symbol,
                "strategy": STRATEGY_RSI,
                "score": rsi,
                "entry_price": entry_p,
                "rsi": round(rsi, 1),
                "previous_rsi": round(prev_rsi, 1),
                "atr": atr,
                "stop_loss_pct": stop,
                "trailing_gap_pct": trail,
            })
        elif is_vbo_signal:
            entry_p = curr["close"]
            stop, trail = _risk_fields(atr, entry_p)
            breakout_pct = (curr["close"] - target_price) / target_price * 100
            candidates.append({
                "symbol": symbol,
                "strategy": STRATEGY_VBO,
                "score": 50.0 + breakout_pct,
                "entry_price": entry_p,
                "rsi": round(rsi, 1),
                "volume_pace_ratio": round(curr["vol"] / expected_volume, 2),
                "atr": atr,
                "stop_loss_pct": stop,
                "trailing_gap_pct": trail,
            })

    candidates.sort(key=lambda x: x["score"])
    return candidates


def confirm_persistent_signals(signals: list[dict], confirmations: dict,
                               required: int = SIGNAL_CONFIRMATIONS
                               ) -> tuple[list[dict], dict]:
    """연속 루프에서 동일하게 유지된 신호만 반환한다."""
    current_keys = {(sig["symbol"], sig["strategy"]) for sig in signals}
    confirmations = {
        key: count for key, count in confirmations.items() if key in current_keys
    }
    for key in current_keys:
        confirmations[key] = confirmations.get(key, 0) + 1
    confirmed = [
        sig for sig in signals
        if confirmations.get((sig["symbol"], sig["strategy"]), 0) >= required
    ]
    return confirmed, confirmations


def day_progress(now: datetime) -> float:
    """UTC 하루 중 경과 비율 (0.0~1.0)."""
    now = now.astimezone(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (now - midnight).total_seconds() / _SECONDS_PER_DAY


def in_entry_blackout(now: datetime) -> bool:
    """UTC 00:00부터 ENTRY_BLACKOUT_MINUTES분 동안 True."""
    now = now.astimezone(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now - midnight < timedelta(minutes=ENTRY_BLACKOUT_MINUTES)
