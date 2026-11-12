from config import RSI_PERIOD, RSI_BUY_THRESHOLD, VBO_K

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

def evaluate_signals(candles_dict):
    """
    전 종목의 캔들 데이터를 분석하여 매수 신호 탐색 및 우선순위 랭킹 반환
    candles_dict: {name: [{"open", "high", "low", "close", "vol"}, ...]}
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
            # RSI가 낮을수록 더 높은 우선순위 (점수가 낮을수록 1순위)
            candidates.append({
                "name": name,
                "strategy": "RSI 과매도 반등",
                "score": rsi,
                "entry_price": curr["close"],
                "rsi": round(rsi, 1)
            })
        elif is_vbo_signal:
            # 돌파 강도
            breakout_pct = (curr["high"] - target_price) / target_price * 100
            candidates.append({
                "name": name,
                "strategy": "변동성 돌파 (추세)",
                "score": 50.0 + breakout_pct,
                "entry_price": target_price,
                "rsi": round(rsi, 1)
            })
            
    # 우선순위 정렬 (RSI 과매도 신호 최우선 배치)
    candidates.sort(key=lambda x: x["score"])
    return candidates
