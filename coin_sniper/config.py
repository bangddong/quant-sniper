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
PROTECT_TRIGGER_PCT = 0.06; PROTECT_LOCK_PCT = 0.015
TRAIL_TRIGGER_PCT = 0.12; MIN_TRAIL_GAP_PCT = 0.04; MAX_TRAIL_GAP_PCT = 0.08
MAX_HOLD_HOURS = 168
MAX_ALLOC_PER_SYMBOL = 0.30; MAX_POSITIONS = 3
MAX_RISK_PER_TRADE_PCT = 0.007; MAX_OPEN_RISK_PCT = 0.02
DAILY_KILL_SWITCH_PCT = 0.04
EQUITY_SLOWDOWN_DRAWDOWN_PCT = 0.08; EQUITY_HALT_DRAWDOWN_PCT = 0.12
UNIVERSE_TOP_N = 20; UNIVERSE_MIN_COUNT = 5; MIN_HISTORY_DAYS = 70
UNIVERSE_RETRY_SECONDS = 600; UNIVERSE_FETCH_PAUSE_SECONDS = 0.1
MIN_PREV_DAY_QUOTE_VOLUME = 20_000_000
EXCLUDED_BASES = frozenset({"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "EURI", "AEUR", "PAXG", "WBTC", "WBETH"})
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
ENTRY_BLACKOUT_MINUTES = 15; SIGNAL_CONFIRMATIONS = 3
POLL_INTERVAL_SECONDS = 30; QUOTE_MAX_AGE_SECONDS = 15
FEED_RECONNECT_DELAY_SECONDS = 3
ORDER_CONFIRM_TIMEOUT_SECONDS = 20; CANDLE_COUNT = 70   # 주문 하나의 체결 확인 전체 기한(단조 시계)
ORDER_CONFIRM_AFTER_SEND_FAILURE_SECONDS = 5   # 전송이 일시 오류로 실패한 주문의 확인 기한
INTENT_NOT_FOUND_GRACE_SECONDS = 60     # 조회에 안 보이는 주문을 "주문된 적 없음"으로 볼 때까지의 유예
PROTECTIVE_STOPS_ENABLED = True          # Task 3 결과로 확정
STOP_LIMIT_OFFSET_PCT = 0.01             # 손절 주문의 지정가 = 발동가 × (1 - 0.01)
STOP_REPLACE_MIN_MOVE_PCT = 0.005
RECON_ESCALATE_SECONDS = 600
SLIPPAGE_ESTIMATE_PCT = 0.001
RECV_WINDOW_MS = 5000; HTTP_TIMEOUT_SECONDS = 10; DEFAULT_BAN_RETRY_SECONDS = 60
EVENT_RETENTION_DAYS = 7; METRICS_RETENTION_DAYS = 400
DASHBOARD_MIN_INTERVAL_SECONDS = 300; DASHBOARD_HEARTBEAT_SECONDS = 1800   # 변화 있을 때 / 변화 없어도
DASHBOARD_PUBLISH_TIMEOUT_SECONDS = 60   # git 명령 하나의 최대 시간(멈춘 push가 매매를 막지 않게)

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
    if mode == "live" and not _flag(env.get("LIVE_TRADING_ENABLED", "false")):
        raise SettingsError("live 모드는 LIVE_TRADING_ENABLED=true 가 필요합니다")
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
    )
