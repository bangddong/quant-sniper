"""코인 봇 설정. 모든 수치 파라미터는 이 파일에만 둔다."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

QUOTE_ASSET = "USDT"; MARKET_PROXY_SYMBOL = "BTCUSDT"
RSI_PERIOD = 14; RSI_BUY_THRESHOLD = 28.0; RSI_RECOVERY_THRESHOLD = 30.0
VBO_K = 0.5; MARKET_MA_PERIOD = 60; VOLUME_CONFIRM_MULTIPLIER = 1.5
MAX_ENTRY_DAY_RETURN_PCT = 0.15; MAX_UPPER_WICK_RATIO = 0.40
ATR_PERIOD = 14; ATR_STOP_MULTIPLIER = 1.5
MIN_STOP_LOSS_PCT = 0.04; MAX_STOP_LOSS_PCT = 0.10
PROTECT_TRIGGER_PCT = 0.04; PROTECT_LOCK_PCT = 0.015   # 2026-11-26 백테스트: 0.06 → 0.04 (조정 2년·검증 1년 모두 개선)
TRAIL_TRIGGER_PCT = 0.12; MIN_TRAIL_GAP_PCT = 0.04; MAX_TRAIL_GAP_PCT = 0.08
MAX_HOLD_HOURS = 168
MAX_ALLOC_PER_SYMBOL = 0.30; MAX_POSITIONS = 3
MAX_RISK_PER_TRADE_PCT = 0.007; MAX_OPEN_RISK_PCT = 0.02
DAILY_KILL_SWITCH_PCT = 0.04
EQUITY_SLOWDOWN_DRAWDOWN_PCT = 0.08; EQUITY_HALT_DRAWDOWN_PCT = 0.12
DRAWDOWN_AUTO_RESET_DAYS = 14   # 낙폭 중단 중 포지션·미확정 주문 없이 이만큼 지나면 고점을 현재 평가액으로 재설정
UNIVERSE_TOP_N = 20; UNIVERSE_MIN_COUNT = 5; MIN_HISTORY_DAYS = 70
UNIVERSE_RETRY_SECONDS = 600; UNIVERSE_FETCH_PAUSE_SECONDS = 0.1
MIN_PREV_DAY_QUOTE_VOLUME = 20_000_000
EXCLUDED_BASES = frozenset({"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "EURI", "AEUR", "PAXG", "WBTC", "WBETH",
                            # 2026-11-26 상위 거래대금에 들어온 스테이블·원자재(태그 조회가 안 될 때도 빠지게 고정)
                            "USD1", "RLUSD", "U", "USDE", "USDS", "XUSD", "BFUSD", "RWUSD", "XAUT"})
# 바이낸스 분류 태그로 빼는 것: 토큰화 주식(bStocks, 신규 상장이 잦아 이름 목록으로는 못 따라간다)·스테이블·원자재.
# 출처는 문서화되지 않은 공개 웹 API다. 실패하면 마지막 성공 결과를 쓰고, 그것도 없으면 감시 종목을 정하지 않는다.
EXCLUDED_TAGS = frozenset({"bStocks", "stablecoin", "tCommodities"})
ASSET_TAGS_URL = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products?includeEtf=true"
ASSET_TAGS_MIN_ENTRIES = 100   # 이보다 적으면 잘린 응답으로 보고 실패 처리
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
ENTRY_BLACKOUT_MINUTES = 15; SIGNAL_CONFIRMATIONS = 3
POLL_INTERVAL_SECONDS = 30; QUOTE_MAX_AGE_SECONDS = 15
FEED_RECONNECT_DELAY_SECONDS = 3
ORDER_CONFIRM_TIMEOUT_SECONDS = 20; CANDLE_COUNT = 70   # 주문 하나의 체결 확인 전체 기한(단조 시계)
ORDER_CONFIRM_AFTER_SEND_FAILURE_SECONDS = 5   # 전송이 일시 오류로 실패한 주문의 확인 기한
INTENT_NOT_FOUND_GRACE_SECONDS = 60     # 조회에 안 보이는 주문을 "주문된 적 없음"으로 볼 때까지의 유예
PROTECTIVE_STOPS_ENABLED = True          # 데모 확인 스크립트(3번) 결과로 확정할 것 — 아직 미확인
PROTECTIVE_STOP_TYPE = "STOP_LOSS"       # 거래소 보호 주문: STOP_LOSS = 발동 시 시장가(갭에도 체결) | STOP_LOSS_LIMIT
STOP_LIMIT_OFFSET_PCT = 0.01             # STOP_LOSS_LIMIT일 때만: 지정가 = 발동가 × (1 - 0.01)
STOP_REPLACE_MIN_MOVE_PCT = 0.005
RECON_ESCALATE_SECONDS = 600
SLIPPAGE_ESTIMATE_PCT = 0.001
RECV_WINDOW_MS = 5000; HTTP_TIMEOUT_SECONDS = 10; DEFAULT_BAN_RETRY_SECONDS = 60
EVENT_RETENTION_DAYS = 7; METRICS_RETENTION_DAYS = 400
DASHBOARD_MIN_INTERVAL_SECONDS = 300; DASHBOARD_HEARTBEAT_SECONDS = 1800   # 변화 있을 때 / 변화 없어도
DASHBOARD_PUBLISH_TIMEOUT_SECONDS = 60   # git 명령 하나의 최대 시간(멈춘 push가 매매를 막지 않게)
DASHBOARD_BRANCH = "dashboard"           # 게시 브랜치 기본값(환경 변수 COIN_DASHBOARD_BRANCH로 바꾼다)

# ---- 추세 전략(COIN_STRATEGY=trend): BTC·ETH 시계열 모멘텀, 주 1회 판단 ----
# 근거: 2026-11-27 사전 고정 백테스트(2018-01~2026-11, 8개 기간). 구현형(월요일 판단·줄이기만·비상 손절 25%)
# t +3.35, 연 +18%, 최대 낙폭 27% (BTC 보유 연 +18%, 낙폭 81%). 파라미터를 결과에 맞춰 고치지 않는다.
STRATEGY_BREAKOUT = "breakout"; STRATEGY_TREND = "trend"
TREND_SYMBOLS = ("BTCUSDT", "ETHUSDT")
TREND_LOOKBACK_DAYS = 28                 # 확정 종가가 28일 전 종가보다 높으면 보유
TREND_VOL_WINDOW_DAYS = 30               # 일간 수익률 30개의 표준편차 × √365 = 연환산 변동성
TREND_VOL_TARGET = 0.10                  # 코인당 비중 = min(TREND_MAX_WEIGHT, 0.10 / 연환산 변동성)
TREND_MAX_WEIGHT = 0.50
TREND_REBALANCE_WEEKDAY = 0              # 월요일(UTC). 판단은 전일까지의 확정 종가로
TREND_TRIM_RATIO = 1.5                   # 보유 가치가 목표의 1.5배를 넘으면 목표까지 일부 매도(늘리는 매수는 하지 않는다)
TREND_STOP_LOSS_PCT = 0.25               # 거래소 비상 손절(고정, 따라 올라가지 않는다). 전략 청산은 주간 판단이 한다
TREND_EQUITY_HALT_DRAWDOWN_PCT = 0.35    # 추세 전략의 낙폭 중단(백테스트 최대 27%보다 넓게: 이걸 넘으면 가정이 깨진 것)

_ENDPOINTS = {
    "demo": ("https://demo-api.binance.com", "wss://demo-stream.binance.com/ws"),
    "live": ("https://api.binance.com", "wss://stream.binance.com:9443/ws"),
}
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TRUE = {"1", "true", "yes", "on"}


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    mode: str
    rest_base: str
    stream_base: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    state_dir: Path
    starting_capital: float
    auto_git_push: bool
    dashboard_branch: str = DASHBOARD_BRANCH
    # 데모 전용: BTC 시장 필터를 건너뛴다. 수익 검증이 아니라 하락장에서도 주문·청산 흐름을 실제로 돌려 보기 위한 것.
    demo_ignore_market_filter: bool = False
    strategy: str = STRATEGY_BREAKOUT       # COIN_STRATEGY: breakout(일봉 돌파·RSI, 기본) | trend(BTC·ETH 주간 추세)

    def require_keys(self) -> None:
        for value in (self.api_key, self.api_secret):
            if not value or value.startswith("your_"):
                raise SettingsError(f"{self.mode} 모드용 Binance API 키/시크릿이 설정되지 않았습니다")


def _flag(value: str) -> bool:
    return value.strip().lower() in _TRUE


def load_settings(env: Mapping[str, str]) -> Settings:
    mode = env.get("MODE", "demo").strip().lower() or "demo"
    if mode not in _ENDPOINTS:
        raise SettingsError(f"알 수 없는 MODE: {mode!r} (demo 또는 live)")
    # 코인 봇 전용 실거래 스위치. 주식 봇과 같이 쓰는 LIVE_TRADING_ENABLED는 보지 않는다.
    if mode == "live" and not _flag(env.get("COIN_LIVE_TRADING_ENABLED", "false")):
        raise SettingsError("live 모드는 COIN_LIVE_TRADING_ENABLED=true 가 필요합니다 "
                            "(주식 봇의 LIVE_TRADING_ENABLED는 코인 봇에 쓰이지 않습니다)")
    ignore_market = _flag(env.get("COIN_DEMO_IGNORE_MARKET_FILTER", "false"))
    if ignore_market and mode != "demo":
        raise SettingsError("COIN_DEMO_IGNORE_MARKET_FILTER 는 demo 모드에서만 쓸 수 있습니다")
    strategy = env.get("COIN_STRATEGY", STRATEGY_BREAKOUT).strip().lower() or STRATEGY_BREAKOUT
    if strategy not in (STRATEGY_BREAKOUT, STRATEGY_TREND):
        raise SettingsError(f"알 수 없는 COIN_STRATEGY: {strategy!r} ({STRATEGY_BREAKOUT} 또는 {STRATEGY_TREND})")
    if ignore_market and strategy != STRATEGY_BREAKOUT:
        raise SettingsError("COIN_DEMO_IGNORE_MARKET_FILTER 는 breakout 전략에서만 쓸 수 있습니다")
    prefix = f"BINANCE_{mode.upper()}"
    rest_base, stream_base = _ENDPOINTS[mode]
    try:
        capital = float(env.get("STARTING_CAPITAL_USDT", "1000"))
    except ValueError:
        raise SettingsError("STARTING_CAPITAL_USDT 가 숫자가 아닙니다") from None
    return Settings(
        mode=mode,
        rest_base=rest_base,
        stream_base=stream_base,
        api_key=env.get(f"{prefix}_API_KEY", "").strip(),
        api_secret=env.get(f"{prefix}_API_SECRET", "").strip(),
        state_dir=_REPO_ROOT / "state" / mode,
        starting_capital=capital,
        auto_git_push=_flag(env.get("AUTO_GIT_PUSH", "false")),
        dashboard_branch=env.get("COIN_DASHBOARD_BRANCH", "").strip() or DASHBOARD_BRANCH,
        demo_ignore_market_filter=ignore_market,
        strategy=strategy,
    )
