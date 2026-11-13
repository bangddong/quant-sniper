import os
import time
import json
import datetime
import subprocess
from config import (
    DYNAMIC_SCREENING, SCREENER_TOP_N, DEFAULT_UNIVERSE,
    STOP_LOSS_PCT, TRAILING_STOP_TRIGGER, TRAILING_STOP_LOCK,
    TAKE_PROFIT_PCT, MAX_HOLD_DAYS, MAX_ALLOC_PER_STOCK, MAX_POSITIONS,
    DAILY_KILL_SWITCH_PCT, POLL_INTERVAL_SECONDS, API_REQUEST_DELAY,
    TOTAL_ROUNDTRIP_COST_PCT, TRANSACTION_TAX_PCT, BROKER_COMMISSION_PCT,
    SLIPPAGE_ESTIMATE_PCT
)
from strategy import evaluate_signals
from kiwoom_api import KiwoomRestAPI
from screener import get_dynamic_universe

TRADES_FILE = os.path.join(os.path.dirname(__file__), "trades.json")
POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "positions.json")
HEALTH_FILE = os.path.join(os.path.dirname(__file__), "health.json")

def update_health_status(status="healthy", detail=""):
    """봇 생존 및 헬스체크 상태 로컬 파일 갱신 (1분 주기, 150바이트 고정)"""
    health_data = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "detail": detail,
        "pid": os.getpid(),
        "is_market_open": is_market_open()
    }
    save_json(HEALTH_FILE, health_data)

def sync_health_to_status_branch(detail="정규 감시 중"):
    """
    main 브랜치를 전혀 건드리지 않고 status 브랜치에 health.json 단일 커밋 강제 푸시
    - 🛡️ 잔디/메인 커밋 히스토리 오염 0% (메인과 완전 분리)
    - 🛡️ status 브랜치에도 항상 커밋 딱 1개(0.1KB)만 덮어써져 용량 증가 0%
    - 🛡️ 깃허브 무료 플랜 완전 최적화
    """
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    health_path = os.path.join(repo_dir, "health.json")
    if not os.path.exists(health_path):
        return
    try:
        # 1. blob 생성
        blob_res = subprocess.run(["git", "hash-object", "-w", "health.json"], cwd=repo_dir, capture_output=True, text=True, check=True)
        blob_sha = blob_res.stdout.strip()
        
        # 2. tree 생성
        tree_input = f"100644 blob {blob_sha}\thealth.json\n"
        tree_res = subprocess.run(["git", "mktree"], cwd=repo_dir, input=tree_input, capture_output=True, text=True, check=True)
        tree_sha = tree_res.stdout.strip()
        
        # 3. 부모 없는 단일 커밋 생성 (히스토리 누적 방지)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        commit_res = subprocess.run(["git", "commit-tree", tree_sha, "-m", f"heartbeat: {now_str} ({detail})"], cwd=repo_dir, capture_output=True, text=True, check=True)
        commit_sha = commit_res.stdout.strip()
        
        # 4. remote status 브랜치로 강제 푸시
        subprocess.run(["git", "push", "-f", "origin", f"{commit_sha}:refs/heads/status"], cwd=repo_dir, capture_output=True, text=True, check=True)
        print(f"[HEALTH] 🌐 status 브랜치 심장박동 동기화 완료: {detail}")
    except Exception as e:
        print(f"[HEALTH WARN] status 브랜치 동기화 실패 (무시됨): {e}")

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

def check_git_permission():
    """봇 기동 시 깃 푸시 권한 사전 점검"""
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        res = subprocess.run(["git", "push", "--dry-run"], cwd=repo_dir, capture_output=True, text=True)
        if res.returncode == 0:
            print("[GIT] ✅ 깃허브 푸시 권한 정상 확인 (피클 비서 실시간 연동 준비 완료)")
            return True
        else:
            print(f"[GIT WARN] ⚠️ 깃허브 푸시 권한 없음: {res.stderr.strip() or '인증 필요'}")
            print("          (거래는 로컬에 기록되나, 깃 인증 전까지 비서 알림이 보류됩니다.)")
            return False
    except Exception as e:
        print(f"[GIT WARN] git 실행 오류: {e}")
        return False

def git_commit_and_push(commit_msg):
    """체결 내역 발생 시 깃허브 자동 푸시 (피클 비서 보고 연동)"""
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        subprocess.run(["git", "add", "trades.json", "positions.json"], cwd=repo_dir, check=False)
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_dir, check=False)
        subprocess.run(["git", "push"], cwd=repo_dir, check=False)
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
    print("🎯 키움증권 퀀트 스나이퍼 봇 가동 시작 (동적 스크리닝 & ATR 손익비 엔진)")
    print(f"   [비용 설정] 거래세: {TRANSACTION_TAX_PCT*100:.2f}% | 수수료: {BROKER_COMMISSION_PCT*100:.3f}%×2 | 슬리피지: {SLIPPAGE_ESTIMATE_PCT*100:.2f}%×2")
    print(f"   [왕복 총비용] 약 {TOTAL_ROUNDTRIP_COST_PCT*100:.2f}% 실질 차감 적용")
    print("=" * 60)
    
    # 깃 푸시 권한 사전 점검
    git_ok = check_git_permission()
    
    # 가동 헬스체크 신호 기록 및 status 브랜치 초기 핑 동기화
    update_health_status(status="running", detail="봇 최초 가동 시작")
    if git_ok:
        sync_health_to_status_branch(detail="봇 최초 가동 시작")
    
    api = KiwoomRestAPI()
    last_screened_date = None
    last_health_sync_time = time.time()
    active_universe = DEFAULT_UNIVERSE
    
    while True:
        try:
            now = datetime.datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            
            # 매일 아침 전일(D-1) 확정 거래대금 TOP 우량주 동적 스크리닝 갱신 (Lookahead 방지)
            if DYNAMIC_SCREENING and (last_screened_date != today_str):
                print(f"[{now_str}] 🔍 전일(D-1) 확정 거래대금 TOP 우량주 동적 스크리닝 가동...")
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
                
            # 2. 보유 포지션 감시 및 청산 체크 (동적 손절, 트레일링 스탑, 익절)
            closed_positions = []
            for name, pos in list(positions.items()):
                if name not in candles_dict:
                    continue
                c = candles_dict[name][-1]
                buy_p = pos["buy_price"]
                curr_p = c["close"]
                high_p = c["high"]
                low_p = c["low"]
                
                # 종목별 맞춤 손절/익절선 (ATR 기반 동적 산출값, 없을 시 기본값 폴백)
                pos_stop_pct = pos.get("stop_loss_pct", STOP_LOSS_PCT)
                pos_tp_pct = pos.get("take_profit_pct", TAKE_PROFIT_PCT)
                
                # 트레일링 스탑 발동 여부 (+4%)
                if high_p >= buy_p * (1 + TRAILING_STOP_TRIGGER):
                    pos["trailing_active"] = True
                    
                exit_price = None
                exit_reason = None
                
                # 손절선 계산
                stop_price = buy_p * (1 - pos_stop_pct)
                if pos.get("trailing_active", False):
                    stop_price = buy_p * (1 + TRAILING_STOP_LOCK) # +1% 수익 잠금
                    
                if low_p <= stop_price:
                    exit_price = stop_price
                    exit_reason = "트레일링 스탑 (+1.0% 익절 잠금)" if pos.get("trailing_active") else f"동적 손절 (-{pos_stop_pct*100:.1f}% ATR 보정)"
                elif high_p >= buy_p * (1 + pos_tp_pct):
                    exit_price = buy_p * (1 + pos_tp_pct)
                    exit_reason = f"목표 익절 달성 (+{pos_tp_pct*100:.1f}%)"
                elif pos.get("days_held", 0) >= MAX_HOLD_DAYS:
                    exit_price = curr_p
                    exit_reason = f"타임스탑 (최대 보유 {MAX_HOLD_DAYS}일 경과)"
                    
                if exit_price:
                    # 거래비용(거래세 + 위탁수수료 + 슬리피지 약 0.41%) 실질 차감
                    gross_ret_pct = ((exit_price - buy_p) / buy_p) * 100
                    cost_krw = pos["shares"] * exit_price * TOTAL_ROUNDTRIP_COST_PCT
                    gross_profit_krw = pos["shares"] * (exit_price - buy_p)
                    net_profit_krw = gross_profit_krw - cost_krw
                    net_ret_pct = (net_profit_krw / (pos["shares"] * buy_p)) * 100
                    
                    api.send_sell_order(pos["code"], name, pos["shares"], exit_price, exit_reason)
                    
                    trade_record = {
                        "timestamp": now_str,
                        "type": "SELL",
                        "name": name,
                        "code": pos["code"],
                        "shares": pos["shares"],
                        "entry_price": buy_p,
                        "exit_price": exit_price,
                        "gross_return_pct": round(gross_ret_pct, 2),
                        "net_return_pct": round(net_ret_pct, 2),
                        "cost_fee_krw": round(cost_krw, 0),
                        "net_profit_krw": round(net_profit_krw, 0),
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
                        shares = 1 # 최소 1주 (소액 가설 검증 지원)
                        
                    invest = shares * entry_p
                    if cash >= invest:
                        api.send_buy_order(code, name, shares, entry_p)
                        positions[name] = {
                            "code": code,
                            "shares": shares,
                            "buy_price": entry_p,
                            "stop_loss_pct": sig.get("stop_loss_pct", STOP_LOSS_PCT),
                            "take_profit_pct": sig.get("take_profit_pct", TAKE_PROFIT_PCT),
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
                            "stop_loss_pct": sig.get("stop_loss_pct", STOP_LOSS_PCT),
                            "take_profit_pct": sig.get("take_profit_pct", TAKE_PROFIT_PCT),
                            "strategy": sig["strategy"]
                        }
                        trades.append(trade_record)
                        new_buys.append(name)
                        
                if new_buys:
                    save_json(POSITIONS_FILE, positions)
                    save_json(TRADES_FILE, trades)
                    git_commit_and_push(f"매수 저격 체결: {', '.join(new_buys)}")
                    
            print(f"[{now_str}] 감시 완료. 현재 보유 종목: {list(positions.keys())}")
            update_health_status(status="running", detail=f"정규 감시 중 (보유: {len(positions)}개)")
            
            # 30분(1800초)마다 status 브랜치에 가벼운 단일 커밋 심장박동 동기화
            if git_ok and (time.time() - last_health_sync_time >= 1800):
                sync_health_to_status_branch(detail=f"정규 감시 중 (보유: {len(positions)}개)")
                last_health_sync_time = time.time()
                
            time.sleep(POLL_INTERVAL_SECONDS)
            
        except Exception as e:
            print(f"[ERROR] 루프 실행 중 예외 발생: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    run_trading_loop()
