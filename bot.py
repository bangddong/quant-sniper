import os
import time
import json
import datetime
import subprocess
from config import (
    DYNAMIC_SCREENING, SCREENER_TOP_N, DEFAULT_UNIVERSE,
    STOP_LOSS_PCT, TRAILING_STOP_TRIGGER, TRAILING_STOP_LOCK,
    TAKE_PROFIT_PCT, MAX_HOLD_DAYS, MAX_ALLOC_PER_STOCK, MAX_POSITIONS,
    DAILY_KILL_SWITCH_PCT, POLL_INTERVAL_SECONDS, API_REQUEST_DELAY
)
from strategy import evaluate_signals
from kiwoom_api import KiwoomRestAPI
from screener import get_dynamic_universe

TRADES_FILE = os.path.join(os.path.dirname(__file__), "trades.json")
POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "positions.json")

def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def git_commit_and_push(commit_msg):
    """체결 내역 발생 시 깃허브 자동 푸시 (피클 비서 보고 연동)"""
    try:
        subprocess.run(["git", "add", "trades.json", "positions.json"], check=False)
        subprocess.run(["git", "commit", "-m", commit_msg], check=False)
        subprocess.run(["git", "push"], check=False)
        print(f"[GIT] 자동 커밋 및 푸시 완료: {commit_msg}")
    except Exception as e:
        print(f"[GIT ERR] 푸시 실패 (무시됨): {e}")

def is_market_open():
    """한국 거래소 정규장 개장 여부 체크 (09:00 ~ 15:30, 월~금)"""
    now = datetime.datetime.now()
    if now.weekday() >= 5: # 주말
        return False
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close

def run_trading_loop():
    print("=" * 60)
    print("🎯 키움증권 퀀트 스나이퍼 봇 가동 시작 (동적 스크리닝 활성화)")
    print("=" * 60)
    
    api = KiwoomRestAPI()
    last_screened_date = None
    active_universe = DEFAULT_UNIVERSE
    
    while True:
        try:
            now = datetime.datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            
            # 매일 아침 당일 거래대금 TOP 우량주 동적 스크리닝 갱신
            if DYNAMIC_SCREENING and (last_screened_date != today_str):
                print(f"[{now_str}] 🔍 당일 거래대금 TOP 우량주 동적 스크리닝 가동...")
                screened = get_dynamic_universe(SCREENER_TOP_N)
                if screened:
                    active_universe = screened
                    last_screened_date = today_str
                    print(f"[{now_str}] ✅ 오늘자 감시 유니버스 {len(active_universe)}종목 갱신 완료:")
                    print("   ", ", ".join(list(active_universe.keys())[:10]), "...")
                else:
                    active_universe = DEFAULT_UNIVERSE
                    print(f"[{now_str}] [WARN] 스크리닝 실패로 기본 코어 유니버스로 폴백합니다.")
                    
            # 장 운영 시간 확인
            if not is_market_open():
                print(f"[{now_str}] 정규장 운영 시간 외입니다. 대기 중...")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
                
            positions = load_json(POSITIONS_FILE, {})
            trades = load_json(TRADES_FILE, [])
            
            # 1. 캔들 데이터 조회
            candles_dict = {}
            for name, code in active_universe.items():
                c = api.get_daily_candles(code, 30)
                if c:
                    candles_dict[name] = c
                time.sleep(API_REQUEST_DELAY) # Rate limit 방어
                
            # 2. 보유 포지션 감시 및 청산 체크 (손절, 트레일링 스탑, 익절)
            closed_positions = []
            for name, pos in list(positions.items()):
                if name not in candles_dict:
                    continue
                c = candles_dict[name][-1]
                buy_p = pos["buy_price"]
                curr_p = c["close"]
                high_p = c["high"]
                low_p = c["low"]
                
                # 트레일링 스탑 발동 여부 (+4%)
                if high_p >= buy_p * (1 + TRAILING_STOP_TRIGGER):
                    pos["trailing_active"] = True
                    
                exit_price = None
                exit_reason = None
                
                # 손절선 계산
                stop_price = buy_p * (1 - STOP_LOSS_PCT)
                if pos.get("trailing_active", False):
                    stop_price = buy_p * (1 + TRAILING_STOP_LOCK) # +1% 수익 잠금
                    
                if low_p <= stop_price:
                    exit_price = stop_price
                    exit_reason = "트레일링 스탑 (+1% 익절 잠금)" if pos.get("trailing_active") else "기계적 칼손절 (-2.0%)"
                elif high_p >= buy_p * (1 + TAKE_PROFIT_PCT):
                    exit_price = buy_p * (1 + TAKE_PROFIT_PCT)
                    exit_reason = "목표 익절 달성 (+8.0%)"
                elif pos.get("days_held", 0) >= MAX_HOLD_DAYS:
                    exit_price = curr_p
                    exit_reason = f"타임스탑 (최대 보유 {MAX_HOLD_DAYS}일 경과)"
                    
                if exit_price:
                    ret_pct = ((exit_price - buy_p) / buy_p - 0.002) * 100
                    profit = pos["shares"] * (exit_price - buy_p) - (pos["shares"] * exit_price * 0.002)
                    api.send_sell_order(pos["code"], name, pos["shares"], exit_price, exit_reason)
                    
                    trade_record = {
                        "timestamp": now_str,
                        "type": "SELL",
                        "name": name,
                        "code": pos["code"],
                        "shares": pos["shares"],
                        "entry_price": buy_p,
                        "exit_price": exit_price,
                        "return_pct": round(ret_pct, 2),
                        "profit_krw": round(profit, 0),
                        "reason": exit_reason
                    }
                    trades.append(trade_record)
                    closed_positions.append(name)
                    
            for name in closed_positions:
                del positions[name]
                
            if closed_positions:
                save_json(POSITIONS_FILE, positions)
                save_json(TRADES_FILE, trades)
                git_commit_and_push(f"매도 체결: {', '.join(closed_positions)}")
                
            # 3. 신규 매수 신호 탐색 및 진입 집행
            if len(positions) < MAX_POSITIONS:
                signals = evaluate_signals(candles_dict)
                cash = api.get_cash_balance()
                alloc_cap = (cash + sum(p["shares"] * p["buy_price"] for p in positions.values())) * MAX_ALLOC_PER_STOCK
                
                new_buys = []
                for sig in signals:
                    name = sig["name"]
                    if name in positions:
                        continue
                    if len(positions) >= MAX_POSITIONS:
                        break
                    if cash < alloc_cap * 0.8:
                        break
                        
                    code = active_universe[name]
                    entry_p = sig["entry_price"]
                    shares = int(alloc_cap // entry_p)
                    if shares < 1:
                        shares = 1 # 최소 1주
                        
                    invest = shares * entry_p
                    if cash >= invest:
                        api.send_buy_order(code, name, shares, entry_p)
                        positions[name] = {
                            "code": code,
                            "shares": shares,
                            "buy_price": entry_p,
                            "days_held": 0,
                            "trailing_active": False,
                            "strategy": sig["strategy"]
                        }
                        cash -= invest
                        trade_record = {
                            "timestamp": now_str,
                            "type": "BUY",
                            "name": name,
                            "code": code,
                            "shares": shares,
                            "entry_price": entry_p,
                            "strategy": sig["strategy"]
                        }
                        trades.append(trade_record)
                        new_buys.append(name)
                        
                if new_buys:
                    save_json(POSITIONS_FILE, positions)
                    save_json(TRADES_FILE, trades)
                    git_commit_and_push(f"매수 저격 체결: {', '.join(new_buys)}")
                    
            print(f"[{now_str}] 감시 완료. 현재 보유 종목: {list(positions.keys())}")
            time.sleep(POLL_INTERVAL_SECONDS)
            
        except Exception as e:
            print(f"[ERROR] 루프 실행 중 예외 발생: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    run_trading_loop()
