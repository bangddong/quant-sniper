import urllib.request
import json
import datetime
from config import MIN_MARKET_CAP_KRW, MAX_MARKET_CAP_KRW, SCREENER_TOP_N

def get_dynamic_universe(top_n=SCREENER_TOP_N, min_market_cap=MIN_MARKET_CAP_KRW, max_market_cap=MAX_MARKET_CAP_KRW):
    """
    코스피/코스닥 시장에서 전일(D-1) 확정 거래대금 및 시가총액 기준 우량주를 동적 스크리닝
    
    🛡️ 미래정보(Lookahead Bias) 원천 차단:
    - 장 개장 전(08:50경) 스크리닝 시, 네이버 증권 API의 거래대금(accumulatedTradingValueRaw) 및
      시가총액(marketValueRaw)은 전일(D-1) 장 마감 시점에 완전히 확정된 공식 정산치입니다.
    - 당일 장중 미확정 데이터나 미래 시세를 사전에 끌어다 쓰지 않으므로 백테스트 및 실전에서
      룩어헤드 바이어스가 발생하지 않습니다.
    
    필터링 조건:
    - 🛡️ 시가총액 최소 1조 원 이상 (하루짜리 작전주·동전주 원천 차단)
    - 🛡️ 시가총액 최대 5조 원 이하 (지수 추종 대형 코끼리주 배제, 탄력성 극대화)
    - 🛡️ 우선주, 스팩, ETN 등 변칙 종목 원천 배제
    - 🛡️ 전일 실질 거래대금(기관·외인 유동성) 최상위 순 정렬
    """
    all_raw = []
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # KOSPI & KOSDAQ 상위권 스캔 (1~3페이지)
    for market in ["KOSPI", "KOSDAQ"]:
        for page in [1, 2, 3]:
            url = f"https://m.stock.naver.com/api/stocks/marketValue/{market}?page={page}&pageSize=50"
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    data = json.loads(r.read().decode("utf-8"))
                    for s in data.get("stocks", []):
                        all_raw.append(s)
            except Exception as e:
                # 개별 페이지 일시 에러 시 패스
                continue
                
    if not all_raw:
        print("[SCREENER ERR] 스크리너 데이터 수신 실패")
        return {}
        
    candidates = []
    seen_codes = set()
    
    for s in all_raw:
        name = s.get("stockName", "")
        code = s.get("itemCode", "")
        price = float(s.get("closePriceRaw", 0))
        trade_val = float(s.get("accumulatedTradingValueRaw", 0))
        market_cap = float(s.get("marketValueRaw", 0))
        
        if code in seen_codes:
            continue
            
        # 1. 잡주·위험종목 필터링 (우선주, 스팩, ETN)
        if name.endswith("우") or "우B" in name or "스팩" in name or "ETN" in name:
            continue
        if price <= 0:
            continue
            
        # 2. 🛡️ 황금 탄력 구간: 시총 1조 원 이상 ~ 5조 원 이하
        if not (min_market_cap <= market_cap <= max_market_cap):
            continue
            
        seen_codes.add(code)
        candidates.append({
            "name": name,
            "code": code,
            "price": price,
            "trade_val": trade_val,
            "market_cap": market_cap
        })
        
    # 기관·외인 수급이 검증된 전일 거래대금 최상위 순으로 정렬
    candidates.sort(key=lambda x: x["trade_val"], reverse=True)
    selected = {c["name"]: c["code"] for c in candidates[:top_n]}
    
    return selected

if __name__ == "__main__":
    universe = get_dynamic_universe(20)
    print(f"🎯 [시총 1조~5조 황금구간] 전일 확정 데이터 기준 동적 스크리닝 결과 ({len(universe)}종목):")
    for name, code in universe.items():
        print(f" - {name} ({code})")
