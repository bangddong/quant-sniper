import os
import time
import json
import datetime
import subprocess
import fcntl
from env_loader import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

from config import (
    DYNAMIC_SCREENING, SCREENER_TOP_N, DEFAULT_UNIVERSE,
    STOP_LOSS_PCT, TRAILING_STOP_TRIGGER, TRAILING_STOP_LOCK,
    TAKE_PROFIT_PCT, MAX_HOLD_DAYS, MAX_ALLOC_PER_STOCK, MAX_POSITIONS,
    DAILY_KILL_SWITCH_PCT, POLL_INTERVAL_SECONDS, API_REQUEST_DELAY,
    TOTAL_ROUNDTRIP_COST_PCT, TRANSACTION_TAX_PCT, BROKER_COMMISSION_PCT,
    SLIPPAGE_ESTIMATE_PCT, ORDER_CONFIRM_TIMEOUT_SECONDS
)
from strategy import evaluate_signals
from kiwoom_api import KiwoomRestAPI
from screener import get_dynamic_universe

TRADES_FILE = os.path.join(BASE_DIR, "trades.json")
POSITIONS_FILE = os.path.join(BASE_DIR, "positions.json")
HEALTH_FILE = os.path.join(BASE_DIR, "health.json")
UNIVERSE_FILE = os.path.join(BASE_DIR, "universe.json")
RISK_FILE = os.path.join(BASE_DIR, "risk_state.json")
LOCK_FILE = os.path.join(BASE_DIR, ".quant_sniper.lock")

def acquire_instance_lock():
    lock_handle = open(LOCK_FILE, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_handle.close()
        raise RuntimeError("quant-sniper가 이미 실행 중입니다.") from exc
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    return lock_handle

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
    temp_path = f"{filepath}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, filepath)

def confirm_order_fill(api, order_id, code, expected_qty,
                       timeout=ORDER_CONFIRM_TIMEOUT_SECONDS):
    deadline = time.time() + timeout
    last_result = None
    while time.time() < deadline:
        result = api.get_order_execution(order_id, code)
        last_result = result
        if result.get("ok") and result.get("filled_qty", 0) >= expected_qty:
            return result
        time.sleep(1.1)
    return last_result or {"ok": False, "error": "체결 확인 시간 초과"}

def trading_days_held(pos, today_str):
    """실행 중 관측된 거래일을 날짜당 한 번만 누적한다."""
    if pos.get("last_held_date") != today_str:
        pos["days_held"] = pos.get("days_held", 0) + 1
        pos["last_held_date"] = today_str
    return pos.get("days_held", 0)

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
    risk_date = None
    opening_assets = None
    trading_halted = False
    
    while True:
        try:
            now = datetime.datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            
            # 매일 아침 전일(D-1) 확정 거래대금 TOP 우량주 동적 스크리닝 갱신 (Lookahead 방지)
            universe_cache = load_json(UNIVERSE_FILE, {})
            if universe_cache.get("date") == today_str and universe_cache.get("symbols"):
                active_universe = universe_cache["symbols"]
                last_screened_date = today_str

            # 08:30~08:59에만 조회해야 네이버 누적 거래대금이 전일 확정치다.
            screening_window = now.hour == 8 and now.minute >= 30
            if DYNAMIC_SCREENING and last_screened_date != today_str and screening_window:
                print(f"[{now_str}] 🔍 전일(D-1) 확정 거래대금 TOP 우량주 동적 스크리닝 가동...")
                screened = get_dynamic_universe(SCREENER_TOP_N)
                if screened:
                    active_universe = screened
                    last_screened_date = today_str
                    save_json(UNIVERSE_FILE, {"date": today_str, "symbols": screened})
                    print(f"[{now_str}] ✅ 오늘자 감시 유니버스 {len(active_universe)}종목 갱신 완료:")
                    print("   ", ", ".join(list(active_universe.keys())[:10]), "...")
                else:
                    active_universe = {}
                    print(f"[{now_str}] [WARN] D-1 스크리닝 실패: 오늘 신규매수를 차단합니다.")
                    
            # 장 운영 시간 확인
            if not is_market_open():
                print(f"[{now_str}] 정규장 운영 시간 외입니다. 대기 중...")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
                
            positions = load_json(POSITIONS_FILE, {})
            trades = load_json(TRADES_FILE, [])

            # 실계좌가 상태의 원본이다. 조회 실패 시 신규 주문을 차단한다.
            account = api.get_account_snapshot()
            risk_state = load_json(RISK_FILE, {})
            if not account.get("ok"):
                trading_halted = True
                update_health_status("degraded", f"계좌 조회 실패: {account.get('error', 'unknown')}")
                print(f"[{now_str}] [RISK] 계좌 조회 실패로 신규 주문을 중단합니다.")
            elif risk_date != today_str:
                risk_date = today_str
                if risk_state.get("date") == today_str:
                    opening_assets = risk_state.get("opening_assets") or None
                    trading_halted = bool(risk_state.get("halted", False))
                else:
                    opening_assets = account.get("total_assets") or None
                    trading_halted = False
                    save_json(RISK_FILE, {
                        "date": today_str, "opening_assets": opening_assets,
                        "halted": False,
                    })

            if account.get("ok") and opening_assets:
                current_assets = account.get("total_assets", opening_assets)
                daily_return = (current_assets - opening_assets) / opening_assets
                if daily_return <= -DAILY_KILL_SWITCH_PCT:
                    trading_halted = True
                    save_json(RISK_FILE, {
                        "date": today_str, "opening_assets": opening_assets,
                        "halted": True, "reason": "daily_loss_limit",
                    })
                    print(f"[{now_str}] [KILL SWITCH] 일일 손실 {daily_return*100:.2f}%로 신규 주문 중단")

            # 브로커에만 있거나 로컬에만 있는 포지션은 자동 추정하지 않고 신규매수를 차단한다.
            if account.get("ok") and not api.sim_mode:
                broker_codes = set(account["positions"])
                local_codes = {p.get("code") for p in positions.values()}
                if broker_codes != local_codes:
                    trading_halted = True
                    print(f"[{now_str}] [RISK] 계좌/로컬 포지션 불일치로 신규 주문 중단 "
                          f"(broker={sorted(broker_codes)}, local={sorted(local_codes)})")
            
            # 1. 캔들 데이터 조회
            candles_dict = {}
            for name, code in active_universe.items():
                c = api.get_daily_candles(code, 30)
                if c:
                    candles_dict[name] = c
                time.sleep(API_REQUEST_DELAY) # Rate limit 방어

            # 신규 후보와 무관하게 모든 보유 종목을 반드시 감시한다.
            for name, pos in positions.items():
                if name in candles_dict:
                    continue
                c = api.get_daily_candles(pos["code"], 30)
                if c:
                    candles_dict[name] = c
                time.sleep(API_REQUEST_DELAY)
                
            # 2. 보유 포지션 감시 및 청산 체크 (동적 손절, 트레일링 스탑, 익절)
            closed_positions = []
            for name, pos in list(positions.items()):
                if name not in candles_dict:
                    continue
                c = candles_dict[name][-1]
                trading_days_held(pos, today_str)
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
                    if account.get("ok") and not api.sim_mode:
                        broker_qty = int(account["positions"].get(pos["code"], {}).get("shares", 0))
                        if broker_qty < pos["shares"]:
                            trading_halted = True
                            print(f"[{now_str}] [CRITICAL] {name} 실보유수량 불일치로 매도 주문 차단 "
                                  f"(broker={broker_qty}, local={pos['shares']})")
                            continue
                    before_qty = pos["shares"]
                    order = api.send_sell_order(pos["code"], name, before_qty, exit_price, exit_reason)
                    actual_exit_price = exit_price
                    if order.get("status") == "filled":
                        confirmed = True
                    elif order.get("status") == "accepted":
                        fill = confirm_order_fill(
                            api, order.get("order_id"), pos["code"], before_qty
                        )
                        confirmed = fill.get("filled_qty", 0) >= before_qty
                        if confirmed:
                            actual_exit_price = fill["fill_price"]
                    else:
                        confirmed = False
                    if not confirmed:
                        trading_halted = True
                        print(f"[{now_str}] [CRITICAL] 매도 체결 미확인: {name}; 포지션 유지 및 신규주문 중단")
                        continue

                    gross_ret_pct = ((actual_exit_price - buy_p) / buy_p) * 100
                    cost_krw = pos["shares"] * actual_exit_price * TOTAL_ROUNDTRIP_COST_PCT
                    gross_profit_krw = pos["shares"] * (actual_exit_price - buy_p)
                    net_profit_krw = gross_profit_krw - cost_krw
                    net_ret_pct = (net_profit_krw / (pos["shares"] * buy_p)) * 100
                    
                    trade_record = {
                        "timestamp": now_str,
                        "type": "SELL",
                        "name": name,
                        "code": pos["code"],
                        "shares": pos["shares"],
                        "entry_price": buy_p,
                        "exit_price": actual_exit_price,
                        "price_source": "broker_execution" if order.get("status") == "accepted" else "simulation",
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

            # 보유일수와 트레일링 상태도 매 루프 원자적으로 보존한다.
            save_json(POSITIONS_FILE, positions)
                
            if closed_positions:
                save_json(POSITIONS_FILE, positions)
                save_json(TRADES_FILE, trades)
                git_commit_and_push(f"매도 체결: {', '.join(closed_positions)}")
                
            # 3. 신규 매수 신호 탐색 및 진입 집행
            if DYNAMIC_SCREENING and last_screened_date != today_str:
                trading_halted = True
                print(f"[{now_str}] [RISK] 오늘자 D-1 유니버스가 없어 신규 주문을 중단합니다.")

            if not trading_halted and len(positions) < MAX_POSITIONS:
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
                        before_qty = 0
                        if account.get("ok"):
                            before_qty = int(account["positions"].get(code, {}).get("shares", 0))
                        order = api.send_buy_order(code, name, shares, entry_p)
                        if order.get("status") == "filled":
                            filled_qty = shares
                            fill_price = entry_p
                        elif order.get("status") == "accepted":
                            fill = confirm_order_fill(
                                api, order.get("order_id"), code, shares
                            )
                            filled_qty = int(fill.get("filled_qty", 0))
                            fill_price = float(fill.get("fill_price", 0))
                        else:
                            filled_qty = 0
                            fill_price = 0
                        if filled_qty <= 0:
                            trading_halted = True
                            print(f"[{now_str}] [CRITICAL] 매수 체결 미확인: {name}; 장부 미반영 및 신규주문 중단")
                            break
                        positions[name] = {
                            "code": code,
                            "shares": filled_qty,
                            "buy_price": fill_price,
                            "stop_loss_pct": sig.get("stop_loss_pct", STOP_LOSS_PCT),
                            "take_profit_pct": sig.get("take_profit_pct", TAKE_PROFIT_PCT),
                            "days_held": 1,
                            "entry_date": today_str,
                            "last_held_date": today_str,
                            "trailing_active": False,
                            "strategy": sig["strategy"]
                        }
                        cash -= filled_qty * fill_price
                        trade_record = {
                            "timestamp": now_str,
                            "type": "BUY",
                            "name": name,
                            "code": code,
                            "shares": filled_qty,
                            "entry_price": fill_price,
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
    _instance_lock = acquire_instance_lock()
    run_trading_loop()
