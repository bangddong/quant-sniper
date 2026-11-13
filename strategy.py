from config import (
    RSI_PERIOD, RSI_BUY_THRESHOLD, VBO_K,
    ATR_PERIOD, ATR_STOP_MULTIPLIER, MIN_STOP_LOSS_PCT, MAX_STOP_LOSS_PCT,
    MIN_RISK_REWARD_RATIO, STOP_LOSS_PCT, TAKE_PROFIT_PCT
)

def calculate_rsi(closes, period=RSI_PERIOD):
    """14일 RSI 지표 계산"""
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

def calculate_atr(candles, period=ATR_PERIOD):
    """
    14일 ATR(Average True Range) 변동성 지표 계산
    - 고정 % 손절 대신 종목 고유의 일중 변동폭을 반영하여 휩소(Whipsaw) 방어
    """
    if len(candles) < period + 1:
        return candles[-1]["close"] * 0.03 # 기본 3% 변동성 가정
        
    tr_list = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_c = candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)
        
    if len(tr_list) < period:
        return candles[-1]["close"] * 0.03
        
    return sum(tr_list[-period:]) / period

def evaluate_signals(candles_dict):
    """
    전 종목의 캔들 데이터를 분석하여 매수 신호 탐색 및 우선순위 랭킹 반환
    candles_dict: {name: [{"open", "high", "low", "close", "vol"}, ...]}
    
    💡 퀀트 피드백 반영:
    1. 고정 -2% 손절 대신 ATR 변동성 기반 동적 손절폭(2.5%~4.0%) 산출
    2. 최소 손익비 1:2 원칙에 따라 목표 익절폭을 동적 연동
    """
    candidates = []
    
    for name, candles in candles_dict.items():
        if len(candles) < 25:
            continue
            
        closes = [c["close"] for c in candles]
        curr = candles[-1]
        prev = candles[-2]
        
        rsi = calculate_rsi(closes)
        ma20 = sum(closes[-20:]) / 20.0
        atr = calculate_atr(candles)
        
        # 전략 1: RSI 극단 과매도 반등
        is_rsi_signal = (rsi <= RSI_BUY_THRESHOLD)
        
        # 전략 2: 20일선 위 변동성 돌파
        target_price = curr["open"] + (prev["high"] - prev["low"]) * VBO_K
        is_vbo_signal = (
            (curr["high"] >= target_price) and 
            (curr["close"] > curr["open"]) and 
            (curr["close"] > ma20)
        )
        
        if is_rsi_signal:
            entry_p = curr["close"]
            atr_pct = (atr / entry_p) if entry_p > 0 else 0.03
            
            # 과매도 종목은 변동성이 터진 상태이므로 ATR * 1.5배 손절선 적용 (최소 2.5%, 최대 4.0% 캡)
            dynamic_stop_pct = max(MIN_STOP_LOSS_PCT, min(MAX_STOP_LOSS_PCT, atr_pct * ATR_STOP_MULTIPLIER))
            dynamic_take_profit_pct = max(TAKE_PROFIT_PCT, dynamic_stop_pct * MIN_RISK_REWARD_RATIO)
            
            candidates.append({
                "name": name,
                "strategy": "RSI 과매도 반등",
                "score": rsi,
                "entry_price": entry_p,
                "rsi": round(rsi, 1),
                "atr": round(atr, 0),
                "stop_loss_pct": round(dynamic_stop_pct, 4),
                "take_profit_pct": round(dynamic_take_profit_pct, 4)
            })
        elif is_vbo_signal:
            entry_p = target_price
            atr_pct = (atr / entry_p) if entry_p > 0 else 0.03
            breakout_pct = (curr["high"] - target_price) / target_price * 100
            
            dynamic_stop_pct = max(MIN_STOP_LOSS_PCT, min(MAX_STOP_LOSS_PCT, atr_pct * ATR_STOP_MULTIPLIER))
            dynamic_take_profit_pct = max(TAKE_PROFIT_PCT, dynamic_stop_pct * MIN_RISK_REWARD_RATIO)
            
            candidates.append({
                "name": name,
                "strategy": "변동성 돌파 (추세)",
                "score": 50.0 + breakout_pct,
                "entry_price": target_price,
                "rsi": round(rsi, 1),
                "atr": round(atr, 0),
                "stop_loss_pct": round(dynamic_stop_pct, 4),
                "take_profit_pct": round(dynamic_take_profit_pct, 4)
            })
            
    # 우선순위 정렬 (RSI 과매도 신호 최우선 배치)
    candidates.sort(key=lambda x: x["score"])
    return candidates
