import urllib.request
import json

def get_dynamic_universe(top_n=20, max_price=None):
    """
    코스피/코스닥 시장에서 당일 거래대금 및 시가총액 최상위 우량주를 실시간 동적 스크리닝
    - 우선주, 스팩, ETN 등 잡주 원천 배제
    - 거래대금(유동성) 최상위 순 정렬
    """
    url = "https://m.stock.naver.com/api/stocks/marketValue/KOSPI?page=1&pageSize=100"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[SCREENER ERR] 스크리너 데이터 수신 실패: {e}")
        return {}
        
    candidates = []
    for s in data.get("stocks", []):
        name = s.get("stockName", "")
        code = s.get("itemCode", "")
        price = float(s.get("closePriceRaw", 0))
        trade_val = float(s.get("accumulatedTradingValueRaw", 0))
        
        # 1. 잡주·위험종목 필터링 (우선주, 스팩, ETN)
        if name.endswith("우") or "우B" in name or "스팩" in name or "ETN" in name:
            continue
        if price <= 0:
            continue
            
        # 2. 가격 상한 필터 (소액 분할매수용, 지정 시)
        if max_price and price > max_price:
            continue
            
        candidates.append({
            "name": name,
            "code": code,
            "price": price,
            "trade_val": trade_val
        })
        
    # 당일 거래대금(시장의 돈이 몰리는 주도주) 순으로 내림차순 정렬
    candidates.sort(key=lambda x: x["trade_val"], reverse=True)
    selected = {c["name"]: c["code"] for c in candidates[:top_n]}
    
    return selected

if __name__ == "__main__":
    universe = get_dynamic_universe(20)
    print(f"🎯 오늘자 동적 스크리닝 결과 ({len(universe)}종목):")
    for name, code in universe.items():
        print(f" - {name} ({code})")
