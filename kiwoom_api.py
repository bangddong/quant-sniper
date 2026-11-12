import os
import time
import json
import urllib.request
import urllib.parse

class KiwoomRestAPI:
    """
    키움증권 최신 공식 REST API 클라이언트
    - 의존성 제로: 파이썬 표준 라이브러리(urllib, json)만 사용
    - 지원 환경: Linux, macOS, Windows 완벽 호환
    - 엔드포인트:
      * 토큰 발급: POST /oauth2/token (au10001)
      * 주식 주문: POST /api/dostk/ordr (kt10000: 매수, kt10001: 매도)
      * 예수금 조회: POST /api/dostk/acnt (kt00001)
    """
    def __init__(self, app_key=None, app_secret=None, account_no=None, is_mock=False):
        self.app_key = (app_key or os.getenv("KIWOOM_APP_KEY", "")).strip()
        self.app_secret = (app_secret or os.getenv("KIWOOM_APP_SECRET", "")).strip()
        self.account_no = (account_no or os.getenv("KIWOOM_ACCOUNT_NO", "")).strip()
        self.is_mock = is_mock
        self.base_url = "https://mockapi.kiwoom.com" if is_mock else "https://api.kiwoom.com"
        self.token = None
        self.token_expiry = 0
        
        # 키 미설정 시 안전 시뮬레이션 모드로 자동 폴백
        self.sim_mode = not (self.app_key and self.app_secret and self.app_key != "your_app_key_here")
        if self.sim_mode:
            print("[INFO] 키움 REST API 키가 미설정되어 '시뮬레이션 모드'로 가동됩니다.")

    def get_token(self):
        """OAuth 2.0 접근 토큰 발급 (au10001) 및 캐싱"""
        if self.sim_mode:
            return "sim_token_ok"
            
        if self.token and time.time() < self.token_expiry - 60:
            return self.token

        url = f"{self.base_url}/oauth2/token"
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
                self.token = data.get("token")
                # 기본 만료시간 (보통 86400초/24시간)
                expires_in = int(data.get("expires_in", 86400))
                self.token_expiry = time.time() + expires_in
                print(f"[AUTH] 키움 REST API 접근 토큰 발급 성공 (만료: {expires_in}초)")
                return self.token
        except Exception as e:
            print(f"[ERR] 토큰 발급 실패: {e}")
            return None

    def get_cash_balance(self):
        """계좌 예수금(주문 가능 현금) 조회 (kt00001)"""
        if self.sim_mode:
            return 500000.0  # 시뮬레이션 초기 현금 50만 원

        token = self.get_token()
        if not token:
            return 0.0

        url = f"{self.base_url}/api/dostk/acnt"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": "kt00001"
        }
        body = {
            "acnt_no": self.account_no
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
                # 예수금 또는 주문가능금액 추출
                cash = float(data.get("ord_psbl_cash", data.get("dnca_tot_amt", 0.0)))
                return cash
        except Exception as e:
            print(f"[ERR] 예수금 조회 실패: {e}")
            return 0.0

    def get_daily_candles(self, code, count=30):
        """종목 일봉 캔들 데이터 조회 (네이버 금융 공용 엔드포인트 - API 호출 한도 절약)"""
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

    def send_buy_order(self, code, name, qty, price=0, trade_type="3"):
        """
        주식 매수 주문 집행 (kt10000)
        - trade_type: "0" 보통(지정가), "3" 시장가 (기본값: 체결 지연 방지용 시장가)
        - dmst_stex_tp: "SOR" (최선주문집행) 또는 "KRX"
        """
        if self.sim_mode:
            print(f"[ORDER BUY/SIM] {name}({code}) {qty}주 @ {price:,.0f}원 (모의)")
            return {
                "status": "success",
                "order_id": f"SIM_B_{int(time.time())}",
                "code": code,
                "name": name,
                "qty": qty,
                "price": price
            }

        token = self.get_token()
        if not token:
            return {"status": "error", "message": "토큰 없음"}

        url = f"{self.base_url}/api/dostk/ordr"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": "kt10000"
        }
        body = {
            "dmst_stex_tp": "SOR",  # 거래소 간 최선가 자동 탐색
            "stk_cd": code,
            "ord_qty": str(qty),
            "ord_uv": str(int(price)) if trade_type == "0" else "",
            "trde_tp": trade_type
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                res = json.loads(r.read().decode("utf-8"))
                ret_code = res.get("return_code", -1)
                if ret_code == 0:
                    ord_no = res.get("ord_no", "")
                    print(f"✅ [실전 매수 완료] {name}({code}) {qty}주 | 주문번호: {ord_no}")
                    return {
                        "status": "success",
                        "order_id": ord_no,
                        "code": code,
                        "name": name,
                        "qty": qty,
                        "price": price
                    }
                else:
                    err_msg = res.get("return_msg", "주문 실패")
                    print(f"❌ [매수 거절] {name}({code}): {err_msg}")
                    return {"status": "error", "message": err_msg}
        except Exception as e:
            print(f"[ERR] 매수 주문 예외 발생: {e}")
            return {"status": "error", "message": str(e)}

    def send_sell_order(self, code, name, qty, price=0, reason="손절/익절", trade_type="3"):
        """
        주식 매도 주문 집행 (kt10001)
        - 칼손절 및 이익 보존을 위해 기본 '3' 시장가로 즉시 청산
        """
        if self.sim_mode:
            print(f"[ORDER SELL/SIM] {name}({code}) {qty}주 매도 ({reason}) (모의)")
            return {
                "status": "success",
                "order_id": f"SIM_S_{int(time.time())}",
                "code": code,
                "name": name,
                "qty": qty,
                "price": price,
                "reason": reason
            }

        token = self.get_token()
        if not token:
            return {"status": "error", "message": "토큰 없음"}

        url = f"{self.base_url}/api/dostk/ordr"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": "kt10001"
        }
        body = {
            "dmst_stex_tp": "SOR",
            "stk_cd": code,
            "ord_qty": str(qty),
            "ord_uv": str(int(price)) if trade_type == "0" else "",
            "trde_tp": trade_type
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                res = json.loads(r.read().decode("utf-8"))
                ret_code = res.get("return_code", -1)
                if ret_code == 0:
                    ord_no = res.get("ord_no", "")
                    print(f"🚨 [실전 매도 완료] {name}({code}) {qty}주 | {reason} | 주문번호: {ord_no}")
                    return {
                        "status": "success",
                        "order_id": ord_no,
                        "code": code,
                        "name": name,
                        "qty": qty,
                        "price": price,
                        "reason": reason
                    }
                else:
                    err_msg = res.get("return_msg", "매도 실패")
                    print(f"❌ [매도 거절] {name}({code}): {err_msg}")
                    return {"status": "error", "message": err_msg}
        except Exception as e:
            print(f"[ERR] 매도 주문 예외 발생: {e}")
            return {"status": "error", "message": str(e)}
