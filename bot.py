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
    SLIPPAGE_ESTIMATE_PCT, ORDER_CONFIRM_TIMEOUT_SECONDS,
    MARKET_PROXY_CODE, ENTRY_START_TIME, ENTRY_END_TIME, AUTO_GIT_PUSH,
    MIN_MARKET_CAP_KRW, MAX_MARKET_CAP_KRW
)
from strategy import evaluate_signals
from kiwoom_api import KiwoomRestAPI
from kiwoom_realtime import KiwoomRealtimeFeed

TRADES_FILE = os.path.join(BASE_DIR, "trades.json")
POSITIONS_FILE = os.path.join(BASE_DIR, "positions.json")
HEALTH_FILE = os.path.join(BASE_DIR, "health.json")
UNIVERSE_FILE = os.path.join(BASE_DIR, "universe.json")
RISK_FILE = os.path.join(BASE_DIR, "risk_state.json")
LOCK_FILE = os.path.join(BASE_DIR, ".quant_sniper.lock")
CURRENT_MARKET_OPEN = False

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
        "is_market_open": CURRENT_MARKET_OPEN
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
        return False
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
        return True
    except Exception as e:
        print(f"[HEALTH WARN] status 브랜치 동기화 실패: {e}")
        return False

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

def build_operational_status(trading_halted, risk_reasons, position_count):
    if trading_halted:
        detail = "; ".join(dict.fromkeys(risk_reasons)) or "안전장치에 의해 신규 주문 중단"
        return "halted", detail
    return "running", f"정규 감시 중 (보유: {position_count}개)"

def confirm_persistent_signals(signals, confirmations, required=3):
    """연속 루프에서 동일하게 유지된 신호만 반환한다."""
    current_keys = {(sig["name"], sig["strategy"]) for sig in signals}
    confirmations = {
        key: count for key, count in confirmations.items() if key in current_keys
    }
    for key in current_keys:
        confirmations[key] = confirmations.get(key, 0) + 1
    confirmed = [
        sig for sig in signals
        if confirmations.get((sig["name"], sig["strategy"]), 0) >= required
    ]
    return confirmed, confirmations

def update_position_stop(pos, current_price):
    """현재가로 진입 후 고점을 갱신하고, 절대로 낮아지지 않는 청산선을 계산한다."""
    buy_price = float(pos["buy_price"])
    stop_loss_pct = float(pos.get("stop_loss_pct", STOP_LOSS_PCT))
    highest_price = max(float(pos.get("highest_price", buy_price)), float(current_price))
    pos["highest_price"] = highest_price
    stop_price = buy_price * (1 - stop_loss_pct)

    if highest_price >= buy_price * (1 + TAKE_PROFIT_PCT):
        gap = float(pos.get("trailing_gap_pct", max(0.02, min(0.04, stop_loss_pct / 1.5))))
        pos["trailing_active"] = True
        stop_price = max(float(pos.get("trailing_stop_price", 0)), highest_price * (1 - gap))
        pos["trailing_stop_price"] = stop_price
        reason = "고점 추적 트레일링 스탑"
    elif highest_price >= buy_price * (1 + TRAILING_STOP_TRIGGER):
        stop_price = max(float(pos.get("trailing_stop_price", 0)),
                         buy_price * (1 + TRAILING_STOP_LOCK))
        pos["trailing_stop_price"] = stop_price
        reason = "수익 보호 스탑"
    else:
        reason = f"동적 손절 (-{stop_loss_pct*100:.1f}% ATR 보정)"
    return stop_price, reason

def get_kiwoom_candles(api, realtime, cache, code, now, count=70):
    """일봉은 일 1회 REST로 받고 당일 봉만 키움 실시간 체결로 갱신한다."""
    date_key = now.strftime("%Y%m%d")
    cached = cache.get(code)
    if not cached or cached["date"] != date_key:
        candles = api.get_daily_candles(code, count)
        if not candles:
            return []
        cache[code] = {"date": date_key, "candles": candles}
    candles = [dict(item) for item in cache[code]["candles"]]
    quote = realtime.quote(code)
    if quote:
        live_candle = {
            "date": date_key, "open": quote["open"], "high": quote["high"],
            "low": quote["low"], "close": quote["price"], "vol": quote["volume"],
        }
        if candles and candles[-1].get("date") == date_key:
            candles[-1].update(live_candle)
        else:
            candles.append(live_candle)
    return candles[-count:]

def check_git_permission():
    """봇 기동 시 깃 푸시 권한 사전 점검"""
    if not AUTO_GIT_PUSH:
        print("[GIT] AUTO_GIT_PUSH=false: GitHub 연동이 비활성화되었습니다.")
        return False
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
        subprocess.run(["git", "add", "trades.json", "positions.json"], cwd=repo_dir,
                       capture_output=True, text=True, check=True)
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_dir,
                       capture_output=True, text=True, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir,
                       capture_output=True, text=True, check=True)
        print(f"[GIT] 자동 커밋 및 푸시 완료: {commit_msg}")
        return True
    except Exception as e:
        print(f"[GIT ERR] 체결 내역 push 실패: {e}")
        return False

def is_market_open(now=None, live_status=False):
    """시간과 당일 실시간 상태가 모두 확인된 경우에만 장이 열린 것으로 본다."""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if not market_open <= now <= market_close:
        return False
    return bool(live_status)

def is_entry_window(now=None):
    now = now or datetime.datetime.now()
    start_hour, start_minute = map(int, ENTRY_START_TIME.split(":"))
    end_hour, end_minute = map(int, ENTRY_END_TIME.split(":"))
    start = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    return start <= now <= end

def market_session_progress(now=None):
    now = now or datetime.datetime.now()
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    total = (market_close - market_open).total_seconds()
    return max(0.0, min(1.0, (now - market_open).total_seconds() / total))

def run_trading_loop():
    global CURRENT_MARKET_OPEN
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
    realtime = KiwoomRealtimeFeed(api.get_token)
    realtime.start()
    last_screened_date = None
    last_health_sync_time = time.time()
    active_universe = DEFAULT_UNIVERSE
    risk_date = None
    opening_assets = None
    trading_halted = False
    last_published_state = ("running", "봇 최초 가동 시작")
    signal_confirmations = {}
    daily_candle_cache = {}
    
    while True:
        try:
            now = datetime.datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            risk_reasons = []
            
            # 매일 아침 전일(D-1) 확정 거래대금 TOP 우량주 동적 스크리닝 갱신 (Lookahead 방지)
            universe_cache = load_json(UNIVERSE_FILE, {})
            if universe_cache.get("date") == today_str and universe_cache.get("symbols"):
                active_universe = universe_cache["symbols"]
                last_screened_date = today_str
            realtime.set_symbols(
                list(active_universe.values()) + [MARKET_PROXY_CODE]
                + [p.get("code") for p in load_json(POSITIONS_FILE, {}).values()]
            )
            proxy_quote = realtime.quote(MARKET_PROXY_CODE)
            CURRENT_MARKET_OPEN = is_market_open(
                now, bool(proxy_quote and proxy_quote.get("market_type") == "2")
            )

            # 장전에는 키움 전일 확정 순위를 미리 캐시한다.
            screening_window = now.weekday() < 5 and now.hour == 8 and now.minute >= 30
            if DYNAMIC_SCREENING and last_screened_date != today_str and screening_window:
                print(f"[{now_str}] 🔍 전일(D-1) 확정 거래대금 TOP 우량주 동적 스크리닝 가동...")
                screened = api.get_previous_day_universe(
                    SCREENER_TOP_N, MIN_MARKET_CAP_KRW, MAX_MARKET_CAP_KRW
                )
                if screened:
                    active_universe = screened
                    last_screened_date = today_str
                    save_json(UNIVERSE_FILE, {"date": today_str, "symbols": screened})
                    print(f"[{now_str}] ✅ 오늘자 감시 유니버스 {len(active_universe)}종목 갱신 완료:")
                    print("   ", ", ".join(list(active_universe.keys())[:10]), "...")
                else:
                    active_universe = {}
                    risk_reasons.append("D-1 스크리닝 실패")
                    trading_halted = True
                    alert_detail = "D-1 스크리닝 실패: 오늘 신규 주문 중단"
                    update_health_status("halted", alert_detail)
                    if git_ok and sync_health_to_status_branch(alert_detail):
                        last_published_state = ("halted", alert_detail)
                        last_health_sync_time = time.time()
                    print(f"[{now_str}] [WARN] D-1 스크리닝 실패: 오늘 신규매수를 차단합니다.")

            # 장중 최초 기동 시에도 키움의 '전일' 확정 순위로만 오늘 유니버스를 복구한다.
            intraday_bootstrap = (
                DYNAMIC_SCREENING
                and last_screened_date != today_str
                and CURRENT_MARKET_OPEN
                and is_entry_window(now)
            )
            if intraday_bootstrap:
                print(f"[{now_str}] 🔄 키움 전일 거래대금 기준 장중 유니버스 복구...")
                screened = api.get_previous_day_universe(
                    SCREENER_TOP_N, MIN_MARKET_CAP_KRW, MAX_MARKET_CAP_KRW
                )
                if screened:
                    active_universe = screened
                    last_screened_date = today_str
                    save_json(UNIVERSE_FILE, {
                        "date": today_str, "symbols": screened,
                        "source": "kiwoom_ka10031_previous_day",
                    })
                    print(f"[{now_str}] ✅ 장중 유니버스 {len(screened)}종목 복구 완료")
                else:
                    trading_halted = True
                    risk_reasons.append("키움 전일 유니버스 복구 실패")
                    print(f"[{now_str}] [RISK] 장중 유니버스 복구 실패: 신규 주문 중단")
                    
            # 장 운영 시간 확인
            if not CURRENT_MARKET_OPEN:
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
                risk_reasons.append(f"계좌 조회 실패: {account.get('error', 'unknown')}")
                print(f"[{now_str}] [RISK] 계좌 조회 실패로 신규 주문을 중단합니다.")
            elif risk_date != today_str:
                risk_date = today_str
                if risk_state.get("date") == today_str:
                    opening_assets = risk_state.get("opening_assets") or None
                    trading_halted = bool(risk_state.get("halted", False))
                    if trading_halted:
                        risk_reasons.append(f"지속 중인 거래 중단: {risk_state.get('reason', 'unknown')}")
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
                    risk_reasons.append(f"일일 손실 한도 도달: {daily_return*100:.2f}%")
                    print(f"[{now_str}] [KILL SWITCH] 일일 손실 {daily_return*100:.2f}%로 신규 주문 중단")

            # 브로커에만 있거나 로컬에만 있는 포지션은 자동 추정하지 않고 신규매수를 차단한다.
            if account.get("ok"):
                broker_codes = set(account["positions"])
                local_codes = {p.get("code") for p in positions.values()}
                if broker_codes != local_codes:
                    trading_halted = True
                    risk_reasons.append("계좌/로컬 포지션 불일치")
                    print(f"[{now_str}] [RISK] 계좌/로컬 포지션 불일치로 신규 주문 중단 "
                          f"(broker={sorted(broker_codes)}, local={sorted(local_codes)})")
            
            # 1. 캔들 데이터 조회
            candles_dict = {}
            for name, code in active_universe.items():
                cache_miss = code not in daily_candle_cache
                c = get_kiwoom_candles(api, realtime, daily_candle_cache, code, now)
                if c:
                    candles_dict[name] = c
                if cache_miss:
                    time.sleep(API_REQUEST_DELAY)

            # 신규 후보와 무관하게 모든 보유 종목을 반드시 감시한다.
            for name, pos in positions.items():
                if name in candles_dict:
                    continue
                cache_miss = pos["code"] not in daily_candle_cache
                c = get_kiwoom_candles(
                    api, realtime, daily_candle_cache, pos["code"], now
                )
                if c:
                    candles_dict[name] = c
                if cache_miss:
                    time.sleep(API_REQUEST_DELAY)
                
            # 2. 보유 포지션 감시 및 청산 체크 (동적 손절, 트레일링 스탑, 익절)
            closed_positions = []
            for name, pos in list(positions.items()):
                if name not in candles_dict:
                    continue
                quote = realtime.quote(pos["code"])
                if not quote:
                    risk_reasons.append(f"{name} 실시간 시세 없음")
                    print(f"[{now_str}] [RISK] {name} 실시간 시세가 없어 청산 판단을 보류합니다.")
                    continue
                trading_days_held(pos, today_str)
                buy_p = float(pos["buy_price"])
                curr_p = quote["price"]
                exit_price = None
                exit_reason = None
                stop_price, stop_reason = update_position_stop(
                    pos, quote.get("high_water", curr_p)
                )
                if curr_p <= stop_price:
                    exit_price = stop_price
                    exit_reason = stop_reason
                elif pos.get("days_held", 0) >= MAX_HOLD_DAYS:
                    exit_price = curr_p
                    exit_reason = f"타임스탑 (최대 보유 {MAX_HOLD_DAYS}일 경과)"
                    
                if exit_price:
                    if account.get("ok"):
                        broker_qty = int(account["positions"].get(pos["code"], {}).get("shares", 0))
                        if broker_qty < pos["shares"]:
                            trading_halted = True
                            risk_reasons.append(f"{name} 실보유수량 불일치")
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
                        risk_reasons.append(f"{name} 매도 체결 미확인")
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
                        "price_source": "broker_execution",
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
                if git_ok and not git_commit_and_push(f"매도 체결: {', '.join(closed_positions)}"):
                    trading_halted = True
                    risk_reasons.append("매도 체결 내역 GitHub push 실패")
                
            # 3. 신규 매수 신호 탐색 및 진입 집행
            if DYNAMIC_SCREENING and last_screened_date != today_str:
                trading_halted = True
                risk_reasons.append("오늘자 D-1 유니버스 없음")
                print(f"[{now_str}] [RISK] 오늘자 D-1 유니버스가 없어 신규 주문을 중단합니다.")

            if not is_entry_window(now):
                print(f"[{now_str}] [ENTRY] 신규 진입 허용 시간({ENTRY_START_TIME}~{ENTRY_END_TIME}) 외")

            if not trading_halted and is_entry_window(now) and len(positions) < MAX_POSITIONS:
                market_candles = get_kiwoom_candles(
                    api, realtime, daily_candle_cache, MARKET_PROXY_CODE, now
                )
                signals = evaluate_signals(
                    candles_dict, market_candles, market_session_progress(now)
                )
                # 재기동 직후의 일시적/이미 끝난 신호 추격을 막는다. 동일 전략 신호가
                # 30초 간격으로 3회 연속 유지될 때만 신규 주문 후보로 인정한다.
                signals, signal_confirmations = confirm_persistent_signals(
                    signals, signal_confirmations, required=3
                )
                # 주문 가격도 키움 실시간 체결가로 통일한다. 오래된 시세는 후보에서 제외한다.
                live_signals = []
                for sig in signals:
                    quote = realtime.quote(active_universe.get(sig["name"], ""))
                    if quote:
                        sig = dict(sig)
                        sig["entry_price"] = quote["price"]
                        live_signals.append(sig)
                signals = live_signals
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
                            risk_reasons.append(f"{name} 매수 체결 미확인")
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
                            "highest_price": fill_price,
                            "trailing_gap_pct": max(0.02, min(0.04,
                                sig.get("atr", fill_price * 0.03) / max(fill_price, 1))),
                            "strategy": sig["strategy"]
                        }
                        realtime.reset_high_water(code, fill_price)
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
                    if git_ok and not git_commit_and_push(f"매수 저격 체결: {', '.join(new_buys)}"):
                        trading_halted = True
                        risk_reasons.append("매수 체결 내역 GitHub push 실패")
                    
            print(f"[{now_str}] 감시 완료. 현재 보유 종목: {list(positions.keys())}")
            status, detail = build_operational_status(
                trading_halted, risk_reasons, len(positions)
            )
            update_health_status(status=status, detail=detail)

            # 정상↔위험 상태가 바뀌면 30분 주기를 기다리지 않고 즉시 피클AI에 공개한다.
            current_state = (status, detail)
            if git_ok and current_state != last_published_state:
                if sync_health_to_status_branch(detail=detail):
                    last_published_state = current_state
                    last_health_sync_time = time.time()
            
            # 30분(1800초)마다 status 브랜치에 가벼운 단일 커밋 심장박동 동기화
            if git_ok and (time.time() - last_health_sync_time >= 1800):
                if sync_health_to_status_branch(detail=detail):
                    last_health_sync_time = time.time()
                
            time.sleep(POLL_INTERVAL_SECONDS)
            
        except Exception as e:
            print(f"[ERROR] 루프 실행 중 예외 발생: {e}")
            error_detail = f"루프 예외: {type(e).__name__}: {e}"
            update_health_status(status="error", detail=error_detail)
            if git_ok and sync_health_to_status_branch(detail=error_detail):
                last_published_state = ("error", error_detail)
                last_health_sync_time = time.time()
            time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    _instance_lock = acquire_instance_lock()
    run_trading_loop()
