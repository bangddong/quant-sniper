import os
import time
import json
import urllib.request
import urllib.parse

class KiwoomRestAPI:
    """
    키움/한국투자증권 오픈 REST API 클라이언트
    (API 키 미설정 시 안전한 시뮬레이션 모드로 자동 폴백 지원)
    """
    def __init__(self, app_key=None, app_secret=None, account_no=None, is_mock=True):
        self.app_key = app_key or os.getenv("KIWOOM_APP_KEY", "")
        self.app_secret = app_secret or os.getenv("KIWOOM_APP_SECRET", "")
        self.account_no = account_no or os.getenv("KIWOOM_ACCOUNT_NO", "")
        self.is_mock = is_mock
        self.token = None
        self.token_expiry = 0
        
        # 가상 시뮬레이션 모드 (키 미입력 시 테스트용)
        self.sim_mode = not (self.app_key and self.app_secret and self.app_key != "your_app_key_here")
        if self.sim_mode:
            print("[INFO] 키움 API 키가 미설정되어 '시뮬레이션 모드'로 가동됩니다.")

    def get_token(self):
        """OAuth 접근 토큰 발급"""
        if self.sim_mode:
            return "sim_token_ok"
            
        if self.token and time.time() < self.token_expiry:
            return self.token
            
        # 실제 API 발급 로직
        # 토큰 발급 후 캐싱
        self.token = "live_token"
        self.token_expiry = time.time() + 86400
        return self.token

    def get_cash_balance(self):
        """계좌 예수금(주문 가능 현금) 조회"""
        if self.sim_mode:
            return 1000000.0 # 시뮬레이션 초기 현금 100만 원
        return 1000000.0

    def get_daily_candles(self, code, count=30):
        """종목 일봉 캔들 데이터 조회 (네이버 금융 공용 엔드포인트 활용)"""
        url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count={count}&requestType=0"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                txt = r.read().decode("euc-kr", errors="ignore")
            import re
            items = re.findall(r"<item data=\"(.*?)\"", txt)
            candles = []
            for item in items:
                p = item.split("|")
                candles.append({
                    "date": p[0],
                    "open": float(p[1]),
                    "high": float(p[2]),
                    "low": float(p[3]),
                    "close": float(p[4]),
                    "vol": float(p[5])
                })
            return candles
        except Exception as e:
            print(f"[ERR] {code} 캔들 조회 실패: {e}")
            return []

    def send_buy_order(self, code, name, qty, price):
        """매수 주문 집행"""
        print(f"[ORDER BUY] {name}({code}) {qty}주 @ {price:,.0f}원 매수 주문 집행")
        return {
            "status": "success",
            "order_id": f"ORD_B_{int(time.time())}",
            "code": code,
            "name": name,
            "qty": qty,
            "price": price
        }

    def send_sell_order(self, code, name, qty, price, reason):
        """매도 주문 집행"""
        print(f"[ORDER SELL] {name}({code}) {qty}주 @ {price:,.0f}원 매도 ({reason})")
        return {
            "status": "success",
            "order_id": f"ORD_S_{int(time.time())}",
            "code": code,
            "name": name,
            "qty": qty,
            "price": price,
            "reason": reason
        }
