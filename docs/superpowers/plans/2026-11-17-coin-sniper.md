# Coin Sniper 1단계 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 원본 주식 봇의 룰과 리스크 골격을 따르는 코인 현물 봇을 바이낸스 데모 모드에서 24시간 돌리고, 수익률·오류율 대시보드를 게시한다.

**Architecture:** 전략·리스크·감시 종목 선정은 거래소와 시계를 모르는 순수 함수다. 엔진은 `Exchange`/`Feed` 인터페이스만 쓰고, 바이낸스 구현과 테스트용 가짜 구현이 그 뒤에 있다. 상태는 모드별 디렉터리의 원자적 JSON이고, 모든 거래소 호출은 지표 수집기를 거친다.

**Tech Stack:** Python 3.10+, 표준 라이브러리(`urllib`, `hmac`, `json`, `decimal`, `unittest`), `websockets>=12,<17`. 그 외 의존성 없음.

**Spec:** `docs/superpowers/specs/2026-11-17-coin-sniper-design.md` — 실행자는 계획과 함께 반드시 읽는다. 2단계(백테스트)는 이 계획의 범위가 아니다.

## Global Constraints

- 윈도우와 리눅스 양쪽에서 실행·테스트가 통과해야 한다. `fcntl`, `msvcrt` 등 플랫폼 전용 모듈을 조건 없이 import하지 않는다.
- 원본 파일(`bot.py`, `strategy.py`, `config.py`, `kiwoom_*.py`, `env_loader.py`, `tests/test_*.py`)은 수정하지 않는다. `env_loader.load_dotenv`는 import해서 쓴다.
- 새 코드는 `coin_sniper/`, 진입점은 `coin_bot.py`, 테스트는 `tests/coin/`.
- 테스트 실행: `python -m unittest discover -s tests/coin -v` (저장소 루트에서). 테스트는 네트워크에 접속하지 않는다.
- 시각은 전부 timezone-aware UTC `datetime`. 하루 경계는 UTC 0시. 표시만 KST.
- 엔진·전략·리스크는 `datetime.now()`, `time.time()`을 직접 부르지 않고 인자로 받는다.
- 거래소 호출은 `coin_sniper/exchange/` 안에서만 한다. 다른 모듈은 `urllib`로 거래소에 접속하지 않는다.
- `MODE=demo|live`, 기본 `demo`. 데모 모드에서 실거래소 주소(`api.binance.com`, `stream.binance.com`)에 접속하지 않는다.
- 현물 시장가 롱만. 마진·선물·레버리지 엔드포인트를 호출하지 않는다.
- API 키·시크릿·서명은 로그, 이벤트, 대시보드, 예외 메시지에 넣지 않는다.
- 확인하지 못한 값은 0이나 빈 값으로 대체하지 않는다. 실패는 `ApiResult(ok=False)`로 돌려주고 호출자가 중단 사유를 건다.
- 수치는 전부 `coin_sniper/config.py`에 두고 다른 모듈에 숫자를 직접 쓰지 않는다.
- 커밋 메시지는 원본 관례대로 한국어 한 줄. 각 작업 끝에 커밋한다.

## Review Focus

1. **상태 파일 손상**: `positions.json`이 깨진 채 시작 → 빈 장부로 시작하지 않고 실행을 거부해야 한다. (Task 8)
2. **보유 중 종목 거래 정지**: 종목 상태가 거래 불가로 바뀜 → 매도 거절이 반복돼도 루프가 죽지 않고, 중단 사유와 이벤트 한 건으로 남아야 한다. (Task 11)
3. **며칠 꺼졌다 재시작**: UTC 날짜가 여러 번 바뀐 뒤 재시작 → 168시간 지난 포지션은 첫 루프에서 청산, 당일 기준 평가액과 감시 종목은 새로 잡혀야 한다. (Task 10, 11)
4. **가격·수량 단위**: 손절가가 가격 단위의 배수가 아니거나 수량이 수량 단위의 배수가 아님 → 내림해서 보내야 하고 거절되면 안 된다. (Task 4, 11)
5. **시계 어긋남**: 서명 요청이 시각 오류(-1021)로 거절 → 서버 시각을 다시 맞추고 한 번 재시도해야 한다. (Task 2)

---

## 파일 구조

| 파일 | 책임 |
|---|---|
| `coin_sniper/config.py` | 환경변수, 모드별 주소·키·상태 경로, 모든 수치 |
| `coin_sniper/models.py` | `ApiResult`, `SymbolRules`, `Quote`, `OrderState` |
| `coin_sniper/exchange/base.py` | `Exchange`, `Feed` 프로토콜 |
| `coin_sniper/exchange/binance.py` | 바이낸스 REST 구현 |
| `coin_sniper/exchange/binance_feed.py` | 바이낸스 `kline_1d` 스트림 |
| `coin_sniper/exchange/fake.py` | 테스트용 `FakeExchange`, `FakeFeed` |
| `coin_sniper/probe.py` | 0단계 데모 확인 스크립트 |
| `coin_sniper/strategy.py` | 지표, 진입 신호, 연속 확인 |
| `coin_sniper/risk.py` | 단위 내림, 수량 산정, 낙폭 제어, 손절 래칫, 가상 계좌 |
| `coin_sniper/universe.py` | 감시 종목 선정 |
| `coin_sniper/ledger.py` | `StateStore`, 주문 의도, 이벤트, 단일 실행 잠금 |
| `coin_sniper/metrics.py` | 오류 카운터, 가동률, 평가액 기록 |
| `coin_sniper/halts.py` | 중단 사유 보드 |
| `coin_sniper/engine.py` | 루프 |
| `coin_sniper/dashboard.py` | 렌더링과 게시 |
| `coin_bot.py` | `run`, `reconcile` 명령 |

---

### Task 1: 브랜치, 설정, 공용 타입

**Files:**
- Create: `coin_sniper/__init__.py`, `coin_sniper/config.py`, `coin_sniper/models.py`, `coin_sniper/exchange/__init__.py`, `tests/coin/test_config.py`
- Modify: `.gitignore` (끝에 `state/` 추가), `.env.example` (끝에 코인 항목 추가)

**Interfaces:**
- Produces: 아래 `config` 상수와 `models` 타입. 이후 모든 작업이 이 이름을 쓴다.

- [ ] **Step 1: 브랜치 준비**

```bash
git fetch https://github.com/rundevelrun/quant-sniper.git main
git checkout -b coin-sniper
git merge --ff-only FETCH_HEAD
git add docs/superpowers && git commit -m "docs: 코인 버전 설계와 구현 계획 추가"
```
Expected: `git log -1 --format=%h HEAD~1`이 `cd6dc66`.

- [ ] **Step 2: 실패하는 테스트 작성** (`tests/coin/test_config.py`)

```python
def test_demo_is_default_and_isolated(self):
    cfg = load_settings({})
    self.assertEqual(cfg.mode, "demo")
    self.assertEqual(cfg.rest_base, "https://demo-api.binance.com")
    self.assertEqual(cfg.stream_base, "wss://demo-stream.binance.com/ws")
    self.assertTrue(str(cfg.state_dir).replace("\\", "/").endswith("state/demo"))

def test_live_requires_explicit_enable(self):
    with self.assertRaises(SettingsError):
        load_settings({"MODE": "live", "BINANCE_LIVE_API_KEY": "k", "BINANCE_LIVE_API_SECRET": "s"})

def test_missing_or_placeholder_keys_rejected(self):
    cfg = load_settings({"BINANCE_DEMO_API_KEY": "your_key_here"})
    with self.assertRaises(SettingsError):
        cfg.require_keys()

def test_unknown_mode_rejected(self):
    with self.assertRaises(SettingsError):
        load_settings({"MODE": "paper"})
```

- [ ] **Step 3: 실패 확인** — Run: `python -m unittest discover -s tests/coin -v` / Expected: `ImportError`

- [ ] **Step 4: `config.py` 구현**

`load_settings(env: Mapping[str, str]) -> Settings` (frozen dataclass: `mode, rest_base, stream_base, api_key, api_secret, state_dir: Path, starting_capital, auto_git_push`; 메서드 `require_keys() -> None`). `class SettingsError(RuntimeError)`.

| 환경변수 | 기본 | 비고 |
|---|---|---|
| `MODE` | `demo` | `demo`, `live` 외에는 `SettingsError` |
| `BINANCE_DEMO_API_KEY` / `_SECRET` | 없음 | `your_`로 시작하면 누락으로 본다 |
| `BINANCE_LIVE_API_KEY` / `_SECRET` | 없음 | |
| `LIVE_TRADING_ENABLED` | `false` | `live`인데 참이 아니면 `SettingsError` |
| `STARTING_CAPITAL_USDT` | `1000` | |
| `AUTO_GIT_PUSH` | `false` | |

live 주소: `https://api.binance.com`, `wss://stream.binance.com:9443/ws`. 상태 경로: `<저장소 루트>/state/<mode>`.

모듈 상수(스펙 6.7과 12절):

```python
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
MIN_PREV_DAY_QUOTE_VOLUME = 20_000_000
EXCLUDED_BASES = frozenset({"USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","EURI","AEUR","PAXG","WBTC","WBETH"})
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
ENTRY_BLACKOUT_MINUTES = 15; SIGNAL_CONFIRMATIONS = 3
POLL_INTERVAL_SECONDS = 30; QUOTE_MAX_AGE_SECONDS = 15
ORDER_CONFIRM_TIMEOUT_SECONDS = 20; CANDLE_COUNT = 70
PROTECTIVE_STOPS_ENABLED = True          # Task 3 결과로 확정
STOP_LIMIT_OFFSET_PCT = 0.01             # 손절 주문의 지정가 = 발동가 × (1 - 0.01)
STOP_REPLACE_MIN_MOVE_PCT = 0.005
RECON_ESCALATE_SECONDS = 600
SLIPPAGE_ESTIMATE_PCT = 0.001
EVENT_RETENTION_DAYS = 7; METRICS_RETENTION_DAYS = 400
```

- [ ] **Step 5: `models.py` 구현**

```python
@dataclass
class ApiResult:
    ok: bool
    value: Any = None
    kind: str = "ok"          # "ok" | "transient" | "permanent" | "fatal"
    error: str = ""
    retry_after: float = 0.0

@dataclass(frozen=True)
class SymbolRules:
    symbol: str; base: str; quote: str; trading: bool
    step_size: float; tick_size: float; min_notional: float

@dataclass
class Quote:
    symbol: str; price: float; open: float; high: float; low: float
    volume: float; quote_volume: float
    day: str                  # 이 봉의 UTC 날짜 "YYYY-MM-DD"
    received_at: float        # epoch 초

@dataclass
class OrderState:
    client_id: str; symbol: str; side: str          # "BUY" | "SELL"
    status: str               # "NEW"|"PARTIALLY_FILLED"|"FILLED"|"CANCELED"|"EXPIRED"|"REJECTED"
    exists: bool = True       # 거래소가 모르는 주문이면 False
    executed_qty: float = 0.0
    quote_qty: float = 0.0    # 체결 금액 합계(USDT)
    fees: dict[str, float] = field(default_factory=dict)   # 자산 → 수수료 수량
    @property
    def avg_price(self) -> float: ...        # executed_qty가 0이면 0.0
    @property
    def is_final(self) -> bool: ...          # NEW, PARTIALLY_FILLED가 아니면 True
```

- [ ] **Step 6: 통과 확인 후 커밋** — Run: 위 명령 / Expected: 4 tests OK

```bash
git add coin_sniper tests/coin .gitignore .env.example && git commit -m "feat: 코인 봇 설정과 공용 타입 추가"
```

---

### Task 2: 바이낸스 REST 어댑터

**Files:**
- Create: `coin_sniper/exchange/base.py`, `coin_sniper/exchange/binance.py`, `tests/coin/test_binance_rest.py`

**Interfaces:**
- Consumes: `Settings`, `ApiResult`, `SymbolRules`, `OrderState`
- Produces:

```python
class Exchange(Protocol):
    def server_time(self) -> ApiResult: ...                                   # value: int(ms)
    def symbol_rules(self) -> ApiResult: ...                                  # value: dict[str, SymbolRules]
    def daily_candles(self, symbol: str, limit: int) -> ApiResult: ...        # value: list[dict], 과거→현재, 마지막은 진행 중인 봉
    def balances(self) -> ApiResult: ...                                      # value: dict[str, tuple[float, float]] (free, locked)
    def market_buy(self, symbol: str, qty: float, client_id: str) -> ApiResult: ...    # value: OrderState
    def market_sell(self, symbol: str, qty: float, client_id: str) -> ApiResult: ...
    def place_stop(self, symbol: str, qty: float, stop_price: float, limit_price: float, client_id: str) -> ApiResult: ...
    def cancel_order(self, symbol: str, client_id: str) -> ApiResult: ...
    def get_order(self, symbol: str, client_id: str) -> ApiResult: ...        # 모르는 주문이면 ok=True, value.exists=False
    def open_orders(self, symbol: str | None = None) -> ApiResult: ...        # value: list[OrderState]

class Feed(Protocol):
    def start(self) -> None: ...
    def set_symbols(self, symbols: Iterable[str]) -> None: ...
    def quote(self, symbol: str, max_age: float = 15) -> Quote | None: ...
    def health(self) -> tuple[bool, str]: ...

class BinanceExchange:
    def __init__(self, settings: Settings, on_call: Callable[[str, ApiResult], None] | None = None,
                 opener: Callable = urllib.request.urlopen, clock: Callable[[], float] = time.time): ...
```

캔들 dict 키: `date`("YYYY-MM-DD" UTC), `open`, `high`, `low`, `close`, `vol`(기준 자산 수량), `quote_vol`(USDT).
`on_call(endpoint_name, result)`은 HTTP 호출 한 번마다 정확히 한 번 불린다(재시도 포함, 각각).

- [ ] **Step 1: 실패하는 테스트 작성** — `opener`에 가짜를 넣어 요청 URL·헤더·응답을 통제한다.

```python
def test_signed_request_has_key_header_timestamp_and_signature(self):
    # balances() 호출 → URL에 timestamp, recvWindow=5000, signature(64자 hex), 헤더 X-MBX-APIKEY
def test_symbol_rules_parse_filters(self):
    # LOT_SIZE.stepSize "0.00001", PRICE_FILTER.tickSize "0.01", NOTIONAL.minNotional "5" → SymbolRules
    # NOTIONAL이 없고 MIN_NOTIONAL만 있어도 같은 결과. status != "TRADING" → trading=False
def test_daily_candles_oldest_first_with_utc_date(self):
    # openTime 1731801600000 → date "2024-11-17"
def test_market_buy_sends_full_response_type_and_parses_fills(self):
    # 요청: type=MARKET, newOrderRespType=FULL, newClientOrderId=client_id
    # 응답 fills 2건(commissionAsset "ETH") → executed_qty, quote_qty, fees == {"ETH": 합계}
def test_place_stop_sends_stop_loss_limit(self):
    # type=STOP_LOSS_LIMIT, timeInForce=GTC, stopPrice, price
def test_unknown_order_is_ok_but_not_exists(self):
    # HTTP 400 {"code": -2013} → ok=True, value.exists is False
def test_error_classification(self):
    # HTTP 503 → transient / HTTP 429 + Retry-After: 7 → transient, retry_after == 7
    # {"code": -2010} → permanent / {"code": -1013} → permanent
    # {"code": -2015} → fatal / HTTP 418 → transient, retry_after > 0 / URLError → transient
def test_clock_skew_resyncs_and_retries_once(self):
    # 첫 응답 {"code": -1021}, 그다음 /api/v3/time, 그다음 성공 → ok=True, 호출 3회
def test_on_call_reported_for_every_http_attempt(self):
def test_secret_never_in_error_text(self):
    # 실패 결과의 error에 api_secret, signature 값이 없어야 한다
```

- [ ] **Step 2: 실패 확인** — Expected: `ImportError`

- [ ] **Step 3: 구현**

엔드포인트(모두 `settings.rest_base` 아래): `GET /api/v3/time`, `GET /api/v3/exchangeInfo`, `GET /api/v3/klines?interval=1d`, `GET /api/v3/account`, `POST /api/v3/order`, `GET /api/v3/order`(`origClientOrderId`), `DELETE /api/v3/order`, `GET /api/v3/openOrders`.
서명: 쿼리 문자열의 HMAC-SHA256, `timestamp = clock()*1000 + offset`. 수량·가격은 지수 표기 없이 문자열로 보낸다.
`symbol_rules()`는 `quote == QUOTE_ASSET`인 종목만 돌려준다. `get_order`의 수수료는 주문 응답에 없으므로 `GET /api/v3/myTrades?orderId=`로 채운다.
어댑터는 스스로 재시도하지 않는다. 예외는 -1021 한 번뿐이다.

- [ ] **Step 4: 통과 확인 후 커밋** — `git commit -m "feat: 바이낸스 REST 어댑터 추가"`

---

### Task 3: 데모 모드 확인 (0단계, 사람 확인 관문)

**Files:**
- Create: `coin_sniper/probe.py`, `docs/superpowers/specs/2026-11-17-demo-probe-results.md`(스크립트가 생성)

**Interfaces:**
- Consumes: `BinanceExchange`, `load_settings`
- Produces: 결과 문서, 그리고 `config.PROTECTIVE_STOPS_ENABLED`의 확정값

- [ ] **Step 1: 사용자에게 요청** — `demo.binance.com`에서 API 키를 발급해 `.env`에 `BINANCE_DEMO_API_KEY`, `BINANCE_DEMO_API_SECRET`을 넣어 달라고 한다. 키 값을 대화에 붙여 달라고 하지 않는다. 발급이 안 되면 여기서 멈추고 보고한다.

- [ ] **Step 2: `python -m coin_sniper.probe` 구현** — `MODE=demo`가 아니면 실행을 거부한다. 아래를 순서대로 하고 각 항목을 PASS/FAIL과 관찰값으로 결과 문서에 쓴다. 한 항목이 실패해도 나머지를 계속한다.

1. 서버 시각, 계좌 조회 성공 여부.
2. 초기 잔고 구성(자산별 수량).
3. USDT 거래 가능 종목 수, 그중 일봉 70개 이상이고 전일 거래대금 2천만 USDT 이상인 종목 수.
4. BTCUSDT를 최소 주문 금액의 3배만큼 시장가 매수 → 체결 수량, 수수료 자산, 실제 받은 수량.
5. 받은 수량으로 현재가 -10%에 손절 주문 등록 → 미체결 조회에 보이는지 → 취소.
6. 전량 시장가 매도.
7. `wss://demo-stream.binance.com/ws`에서 `btcusdt@kline_1d`를 구독해 10초 안에 시가·고가·저가·거래량이 오는지.

- [ ] **Step 3: 실행하고 결과 반영**

Run: `python -m coin_sniper.probe` / Expected: 결과 문서 생성.

| 결과 | 조치 |
|---|---|
| 1 실패 | 멈추고 사용자에게 보고 |
| 5 실패 | `PROTECTIVE_STOPS_ENABLED = False`로 바꾸고 진행 |
| 3에서 통과 종목 5개 미만 | 멈추고 사용자에게 보고 |
| 7 실패 | Task 5를 "REST 캔들 주기 조회 피드"로 바꿔 진행하고 사용자에게 알림 |
| 4의 수수료 자산, 최소 주문 금액 | 결과 문서에 기록. 이후 작업의 가정과 다르면 사용자에게 알림 |

- [ ] **Step 4: 커밋** — `git commit -m "feat: 데모 모드 확인 스크립트와 결과 추가"`. **사용자가 결과 문서를 확인한 뒤 다음 작업으로 간다.**

---

### Task 4: 리스크 (순수 함수)

**Files:**
- Create: `coin_sniper/risk.py`, `tests/coin/test_risk.py`

**Interfaces:**
- Consumes: `config` 상수, `SymbolRules`
- Produces:

```python
def floor_to_step(qty: float, step: float) -> float
def floor_to_tick(price: float, tick: float) -> float
def account_value(risk_state: dict, positions: dict, prices: dict[str, float]) -> tuple[float, float]   # (cash, equity)
def position_open_risk(pos: dict) -> float
def drawdown_controls(equity: float, peak: float | None) -> tuple[float, float, float, bool]            # (peak, drawdown, multiplier, halted)
def size_entry(equity: float, cash: float, entry_price: float, stop_loss_pct: float,
               open_risk_total: float, rules: SymbolRules, size_multiplier: float = 1.0) -> float
def update_stop(pos: dict, high_water: float) -> tuple[float, str]
def time_stop_due(pos: dict, now: datetime) -> bool
```

포지션 dict 필드(장부와 공용): `symbol, qty, buy_price, cost, stop_loss_pct, trailing_gap_pct, highest_price, stop_price, trailing_active, entry_time`(ISO UTC)`, strategy, protective`(`None` 또는 `{"client_id", "stop_price"}`).
`risk_state` 필드: `starting_capital, realized_pnl, peak_equity, day, day_open_equity, segment_start`.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
RULES = SymbolRules("XUSDT", "X", "USDT", True, 0.001, 0.01, 5.0)

def test_floor_helpers_never_round_up_and_avoid_float_noise(self):
    self.assertEqual(floor_to_step(0.123456, 0.001), 0.123)
    self.assertEqual(floor_to_step(0.3, 0.1), 0.3)          # 0.30000000000000004 문제 없음
    self.assertEqual(floor_to_tick(93.019, 0.01), 93.01)

def test_account_value_is_virtual(self):
    rs = {"starting_capital": 1000.0, "realized_pnl": 50.0}
    pos = {"A": {"qty": 2.0, "cost": 200.0}}
    self.assertEqual(account_value(rs, pos, {"A": 110.0}), (850.0, 1070.0))

def test_size_by_risk(self):
    self.assertEqual(size_entry(1000, 1000, 100.0, 0.07, 0.0, RULES), 1.0)      # 7 / 7
def test_size_capped_by_allocation(self):
    self.assertEqual(size_entry(1000, 1000, 100.0, 0.01, 0.0, RULES), 3.0)      # 300 / 100
def test_size_capped_by_cash(self):
    self.assertEqual(size_entry(1000, 50, 100.0, 0.07, 0.0, RULES), 0.5)
def test_size_shrinks_to_remaining_open_risk(self):
    self.assertEqual(size_entry(1000, 1000, 100.0, 0.07, 14.0, RULES), 0.857)   # (20-14)/7
def test_size_zero_when_open_risk_full_or_below_min_notional(self):
    self.assertEqual(size_entry(1000, 1000, 100.0, 0.07, 20.0, RULES), 0.0)
    self.assertEqual(size_entry(1000, 4.0, 100.0, 0.07, 0.0, RULES), 0.0)
def test_size_multiplier_halves_risk_and_allocation(self):
    self.assertEqual(size_entry(1000, 1000, 100.0, 0.07, 0.0, RULES, 0.5), 0.5)

def test_open_risk_zero_once_stop_above_entry(self):
    self.assertEqual(position_open_risk({"qty": 2, "buy_price": 100, "stop_price": 93}), 14)
    self.assertEqual(position_open_risk({"qty": 2, "buy_price": 100, "stop_price": 101.5}), 0)

def test_stop_ratchets_and_never_moves_down(self):
    pos = {"buy_price": 100.0, "stop_loss_pct": 0.07, "trailing_gap_pct": 0.05,
           "highest_price": 100.0, "stop_price": 93.0, "trailing_active": False}
    self.assertEqual(update_stop(pos, 105.9)[0], 93.0)
    self.assertEqual(update_stop(pos, 106.0)[0], 101.5)
    self.assertAlmostEqual(update_stop(pos, 112.0)[0], 106.4); self.assertTrue(pos["trailing_active"])
    self.assertAlmostEqual(update_stop(pos, 108.0)[0], 106.4); self.assertEqual(pos["highest_price"], 112.0)

def test_drawdown_slows_then_halts(self):
    self.assertEqual(drawdown_controls(100, 100)[2:], (1.0, False))
    self.assertEqual(drawdown_controls(92, 100)[2:], (0.5, False))
    self.assertEqual(drawdown_controls(88, 100)[2:], (0.5, True))

def test_time_stop_is_168_hours_from_entry(self):
    pos = {"entry_time": "2026-11-10T03:00:00+00:00"}
    self.assertFalse(time_stop_due(pos, datetime(2026, 11, 17, 2, 59, tzinfo=timezone.utc)))
    self.assertTrue(time_stop_due(pos, datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)))
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현** — 내림은 `decimal.Decimal(str(x))`로 한다. `update_stop`과 `drawdown_controls`는 원본 `bot.py`의 `update_position_stop`, `account_drawdown_controls`를 옮기되 상수 이름만 바꾼다. `update_stop`은 `pos["stop_price"]`, `pos["highest_price"]`, `pos["trailing_active"]`를 갱신한다. `size_entry`의 열린 위험 한도는 `equity * MAX_OPEN_RISK_PCT - open_risk_total`이고 `size_multiplier`를 곱하지 않는다.

- [ ] **Step 4: 통과 확인 후 커밋** — `git commit -m "feat: 코인용 리스크 계산 추가"`

---

### Task 5: 전략, 감시 종목 선정 (순수 함수)

**Files:**
- Create: `coin_sniper/strategy.py`, `coin_sniper/universe.py`, `tests/coin/test_strategy.py`, `tests/coin/test_universe.py`

**Interfaces:**
- Consumes: `config` 상수, 캔들 dict(Task 2), `SymbolRules`
- Produces:

```python
# strategy.py
def is_favorable_market(market_candles: list[dict]) -> bool
def calculate_rsi(closes: list[float], period: int = RSI_PERIOD) -> float
def calculate_atr(candles: list[dict], period: int = ATR_PERIOD) -> float
def evaluate_signals(candles_by_symbol: dict[str, list[dict]], market_candles: list[dict],
                     day_progress: float) -> list[dict]
def confirm_persistent_signals(signals: list[dict], confirmations: dict, required: int = SIGNAL_CONFIRMATIONS
                               ) -> tuple[list[dict], dict]
def day_progress(now: datetime) -> float            # UTC 하루 중 경과 비율 0.0~1.0
def in_entry_blackout(now: datetime) -> bool        # UTC 00:00부터 ENTRY_BLACKOUT_MINUTES분 동안 True

# universe.py
def is_excluded(rules: SymbolRules) -> bool
def select_universe(rules: dict[str, SymbolRules], candles_by_symbol: dict[str, list[dict]]) -> list[str]
```

신호 dict 키: `symbol, strategy`(`"rsi_recovery"` 또는 `"vbo"`)`, score, entry_price, atr, stop_loss_pct, trailing_gap_pct`.

- [ ] **Step 1: 실패하는 테스트 작성**

원본 `tests/test_strategy_filters.py`의 네 테스트를 새 이름·키로 옮기고 아래를 더한다.

```python
def test_stop_and_trail_clamped_to_coin_ranges(self):
    # ATR/가격 = 1% 인 신호 → stop_loss_pct == 0.04, trailing_gap_pct == 0.04
    # ATR/가격 = 20% 인 신호 → stop_loss_pct == 0.10, trailing_gap_pct == 0.08
def test_day_surge_over_15_percent_excluded(self):
def test_gap_is_not_a_filter(self):
    # 시가가 전일 종가보다 5% 높아도 다른 조건이 맞으면 신호가 나온다
def test_zero_range_candle_does_not_divide_by_zero(self):
    # high == low 인 당일 봉 → 예외 없이 처리
def test_tiny_priced_coin_signals(self):
    # 가격 0.00001234 수준 종목도 같은 조건이면 신호가 나온다 (원본의 max(range, 1.0)을 쓰지 않는다)
def test_day_progress_and_blackout(self):
    self.assertEqual(day_progress(datetime(2026,11,17,12,0,tzinfo=timezone.utc)), 0.5)
    self.assertTrue(in_entry_blackout(datetime(2026,11,17,0,14,tzinfo=timezone.utc)))
    self.assertFalse(in_entry_blackout(datetime(2026,11,17,0,15,tzinfo=timezone.utc)))

# test_universe.py
def test_ranks_by_previous_completed_day_not_forming_candle(self):
    # A: 전일 quote_vol 9e7, 당일 1e9 / B: 전일 1e8, 당일 1e6 → ["B", "A"]
def test_excludes_stable_leveraged_untradable_short_history_low_volume(self):
    # USDCUSDT, BTCUPUSDT, trading=False, 캔들 60개, 전일 거래대금 1e7 → 모두 제외
def test_returns_at_most_top_n(self):
def test_proxy_symbol_can_be_in_universe(self):   # BTCUSDT 포함
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현** — `strategy.py`는 원본 `strategy.py`를 옮긴다. 바꾸는 것: 갭 필터 삭제, `max(range, 1.0)` 대신 범위가 0이면 윗꼬리 비율 0, `volume_progress` 인자를 `day_progress`로, 손절·추적 폭 범위를 새 상수로, `take_profit_pct` 필드 삭제, 종목 키를 이름이 아니라 심볼로. `select_universe`의 "전일"은 `candles[-2]`다(마지막 봉은 진행 중). 캔들이 없는 종목은 제외한다.

- [ ] **Step 4: 통과 확인 후 커밋** — `git commit -m "feat: 코인용 전략과 감시 종목 선정 추가"`

---

### Task 6: 시세 피드

**Files:**
- Create: `coin_sniper/exchange/binance_feed.py`, `tests/coin/test_binance_feed.py`

**Interfaces:**
- Consumes: `Settings`, `Quote`, `Feed` 프로토콜
- Produces: `class BinanceFeed(Feed)` — `__init__(self, settings, on_reconnect: Callable[[], None] | None = None, clock=time.time)`, 그리고 테스트용 `handle_message(self, raw: str) -> None`

- [ ] **Step 1: 실패하는 테스트 작성** — 소켓 없이 `handle_message`와 `quote`만 검증한다.

```python
def test_kline_message_becomes_quote(self):
    # {"e":"kline","s":"BTCUSDT","k":{"t":1731801600000,"o":"90000","h":"91000","l":"89000","c":"90500","v":"12","q":"1080000"}}
    # → quote("BTCUSDT").price == 90500, .open == 90000, .day == "2024-11-17"
def test_stale_quote_returns_none(self):      # clock을 16초 뒤로 → None
def test_unknown_symbol_returns_none(self):
def test_subscription_payload(self):
    # set_symbols(["BTCUSDT","ETHUSDT"]) → 내부 구독 요청 params == ["btcusdt@kline_1d","ethusdt@kline_1d"]
def test_malformed_message_is_ignored(self):  # 예외 없이 무시
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현** — 원본 `kiwoom_realtime.py`의 스레드+asyncio 구조를 따른다. `settings.stream_base`에 접속해 `{"method":"SUBSCRIBE","params":[...],"id":n}`으로 구독하고, 종목이 바뀌면 빠진 것은 `UNSUBSCRIBE`한다. 끊기면 3초 뒤 재접속하고 `on_reconnect`를 부른다. 진입 후 고점은 피드가 아니라 엔진이 포지션에 기록한다(원본의 `reset_high_water`는 옮기지 않는다).

- [ ] **Step 4: 통과 확인 후 커밋** — `git commit -m "feat: 바이낸스 시세 피드 추가"`

---

### Task 7: 지표 수집, 중단 보드

**Files:**
- Create: `coin_sniper/metrics.py`, `coin_sniper/halts.py`, `tests/coin/test_metrics.py`, `tests/coin/test_halts.py`

**Interfaces:**
- Produces:

```python
# halts.py
ACCOUNT_FETCH, FEED_STALE, MAINTENANCE, RATE_LIMIT, UNIVERSE, DAILY_LOSS, DRAWDOWN, \
RECON_MISMATCH, ORDER_UNCONFIRMED, PROTECTIVE_CANCEL, FATAL = ...   # 문자열 코드
class HaltBoard:
    def set(self, code: str, detail: str, now: datetime) -> None
    def clear(self, code: str) -> None
    def is_set(self, code: str) -> bool
    def active(self) -> list[dict]            # [{"code", "detail", "since"}], 설정 순
    def since(self, code: str) -> datetime | None
    def to_dict(self) -> dict; @classmethod def from_dict(cls, data: dict) -> "HaltBoard"

# metrics.py
class Metrics:
    def __init__(self, data: dict | None = None)
    def record_api(self, endpoint: str, result: ApiResult, now: datetime) -> None
    def record_order(self, event: str, now: datetime) -> None      # "sent"|"rejected"|"unconfirmed"|"partial"|"filled"
    def record_loop(self, ok: bool, now: datetime) -> None
    def record_feed_reconnect(self, now: datetime) -> None
    def add_seconds(self, bucket: str, seconds: float, now: datetime) -> None   # "feed_down" | "halt:<code>" | "offline"
    def record_restart(self, now: datetime) -> None
    def summary(self, now: datetime, days: int | None) -> dict     # days=None이면 전체
    def to_dict(self) -> dict
def append_equity(history: list[dict], day: str, equity: float, btc_close: float) -> list[dict]
def period_returns(history: list[dict], today_equity: float, today_btc: float) -> dict
```

`summary` 키: `api_total, api_failed, api_fail_rate, api_transient, api_permanent, orders_sent, orders_rejected, orders_unconfirmed, orders_partial, feed_reconnects, feed_down_seconds, halt_seconds`(코드별 dict)`, loops_ok, loops_failed, uptime_pct, restarts`.
`uptime_pct` = `loops_ok × POLL_INTERVAL_SECONDS ÷ (기간 초)` × 100, 100 상한.
`period_returns` 키: `1d, 7d, 30d, all` 각각 `{"bot": pct|None, "btc": pct|None}`.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_api_failure_rate_by_kind(self):
    # ok 8회, transient 1회, permanent 1회 → api_total 10, api_failed 2, api_fail_rate 20.0
def test_summary_window_excludes_older_days(self):
def test_retention_drops_days_older_than_400(self):
def test_uptime_from_completed_loops(self):
    # 하루 동안 loops_ok 1440 → uptime_pct 50.0
def test_append_equity_replaces_same_day(self):
def test_period_returns_vs_btc_and_none_when_history_short(self):
def test_halt_board_roundtrip_and_order(self):
def test_set_twice_keeps_original_since(self):
```

- [ ] **Step 2: 실패 확인** → **Step 3: 구현** (카운터는 UTC 날짜별 dict) → **Step 4: 통과 확인 후 커밋** — `git commit -m "feat: 오류 지표와 중단 보드 추가"`

---

### Task 8: 상태 저장, 주문 의도, 이벤트, 잠금

**Files:**
- Create: `coin_sniper/ledger.py`, `tests/coin/test_ledger.py`

**Interfaces:**
- Produces:

```python
class StateCorruptError(RuntimeError): ...
class AlreadyRunningError(RuntimeError): ...
class StateStore:
    def __init__(self, state_dir: Path)
    def load(self, name: str, default)          # 파일이 없으면 default, 깨졌으면 StateCorruptError
    def save(self, name: str, data) -> None     # 임시 파일 → fsync → os.replace
    def path(self, name: str) -> Path
# name: "positions" | "trades" | "intents" | "risk_state" | "universe" | "events" | "metrics" | "equity_history" | "halts" | "dust"
def new_client_id(mode: str, kind: str) -> str                    # 예: "cs-d-en-3f9a1c2b7d10", 36자 이하, [A-Za-z0-9-]만
def add_intent(intents: dict, client_id: str, symbol: str, side: str, qty: float, kind: str, reason: str, now: datetime) -> dict
def record_event(events: list[dict], event: dict, now: datetime) -> list[dict]
class InstanceLock:
    def __init__(self, path: Path); def acquire(self) -> None; def release(self) -> None
```

`kind`: `"entry" | "exit" | "protective"`. 이벤트 `type`: `"BUY" | "SELL" | "SKIP" | "RISK" | "ERROR" | "SYSTEM"`.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_save_is_atomic_and_roundtrips(self):
def test_missing_file_returns_default(self):
def test_corrupt_file_raises_instead_of_default(self):          # Review Focus 1
    path.write_text('{"BTCUSDT": {"qty": 0.0', encoding="utf-8")
    with self.assertRaises(StateCorruptError): store.load("positions", {})
def test_client_id_is_exchange_safe_and_unique(self):
def test_events_keep_seven_days_and_merge_repeated_skips(self):  # 원본 update_dashboard_events와 같은 동작
def test_second_lock_on_same_path_fails(self):
def test_lock_released_can_be_reacquired(self):
```

- [ ] **Step 2: 실패 확인** → **Step 3: 구현** — `record_event`는 원본 `update_dashboard_events`를 옮긴다(키를 `code`에서 `symbol`로). 잠금은 플랫폼 분기 없이 동작하는 방식으로 하고, 죽은 프로세스가 남긴 잠금은 다음 실행이 가져갈 수 있어야 한다. → **Step 4: 통과 확인 후 커밋** — `git commit -m "feat: 상태 저장과 주문 의도 기록 추가"`

---

### Task 9: 가짜 거래소

**Files:**
- Create: `coin_sniper/exchange/fake.py`, `tests/coin/test_fake_exchange.py`

**Interfaces:**
- Produces:

```python
class FakeExchange(Exchange):
    def __init__(self, rules: dict[str, SymbolRules], balances: dict[str, float], fee_rate: float = 0.001)
    def set_price(self, symbol: str, price: float) -> None
    def set_candles(self, symbol: str, candles: list[dict]) -> None
    def fail_next(self, method: str, result: ApiResult, times: int = 1) -> None
    def set_fill_ratio(self, ratio: float) -> None            # 다음 시장가 주문 하나에만 적용
    def drop_response_next(self, method: str) -> None         # 주문은 처리되지만 호출자는 transient 실패를 받는다
    def trigger_stops(self) -> None                           # 현재가가 발동가 이하인 손절 주문을 체결시킨다
    def set_trading(self, symbol: str, trading: bool) -> None
    calls: list[tuple[str, dict]]                             # 기록된 호출
class FakeFeed(Feed):
    def set_quote(self, symbol, price, open=None, high=None, low=None, volume=0.0, day="2026-11-17", age=0.0) -> None
    def set_health(self, connected: bool, error: str = "") -> None
```

동작 규칙: 매수 수수료는 받은 코인에서, 매도 수수료는 USDT에서 뺀다. 손절 주문은 수량을 잠근다. 잠긴 수량을 팔려 하면 `permanent` 실패(-2010). 같은 `client_id`로 다시 주문하면 `permanent` 실패. 최소 주문 금액 미만이나 단위 위반은 `permanent` 실패(-1013). `trading=False`인 종목 주문은 `permanent` 실패.

- [ ] **Step 1: 위 규칙마다 테스트 하나씩 작성** → **Step 2: 실패 확인** → **Step 3: 구현** → **Step 4: 통과 확인 후 커밋** — `git commit -m "test: 가짜 거래소와 피드 추가"`

---

### Task 10: 엔진 — 계좌, 날짜 변경, 감시 종목, 대조, 중단

**Files:**
- Create: `coin_sniper/engine.py`, `tests/coin/test_engine_state.py`, `tests/coin/helpers.py`

**Interfaces:**
- Consumes: Task 1~9의 전부
- Produces:

```python
@dataclass
class LoopReport:
    now: datetime; ok: bool; equity: float | None; cash: float | None
    entry_allowed: bool; halts: list[dict]; events: list[dict]      # 이번 루프에서 새로 생긴 이벤트
    raw_signals: int = 0; confirmed_signals: int = 0
class Engine:
    def __init__(self, exchange: Exchange, feed: Feed, store: StateStore, metrics: Metrics,
                 settings: Settings, sleep: Callable[[float], None] = time.sleep)
    def startup(self, now: datetime) -> None
    def run_once(self, now: datetime) -> LoopReport
    def snapshot(self, now: datetime) -> dict                       # 대시보드 입력 (Task 13이 키를 정의)
```

`tests/coin/helpers.py`: `make_engine(**overrides) -> tuple[Engine, FakeExchange, FakeFeed, StateStore]`, `trending_candles(n, start, step)`, `utc(y, m, d, h=0, mi=0)`.

`run_once` 단계 순서(이후 작업이 여기에 끼워 넣는다):
1. 진행 중 주문 의도 해소 (Task 11)
2. 날짜 변경 처리: `risk_state.day` 갱신, `day_open_equity` 재설정, `DAILY_LOSS` 해제, 평가액 기록 한 줄, 감시 종목 재선정
3. 계좌 조회 → 실패 시 `ACCOUNT_FETCH`
4. 장부 대조 → 불일치 시 `RECON_MISMATCH`
5. 캔들 준비(하루 한 번 REST, 당일 봉은 피드로 덮어씀. 원본 `get_kiwoom_candles`와 같은 방식)
6. 청산과 보호 주문 관리 (Task 11)
7. 평가액, 낙폭, 일일 손실 → `DRAWDOWN`, `DAILY_LOSS`
8. 진입 (Task 12)
9. 상태 저장, `metrics.record_loop`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_first_run_initializes_virtual_account(self):
    # risk_state.starting_capital == 1000, day == 오늘, day_open_equity == 1000
def test_account_fetch_failure_sets_halt_and_recovery_clears_it(self):
    ex.fail_next("balances", ApiResult(False, kind="transient", error="502"))
    r1 = eng.run_once(t0); self.assertFalse(r1.entry_allowed)
    r2 = eng.run_once(t0 + 30s); self.assertNotIn(ACCOUNT_FETCH, codes(r2))
def test_universe_selected_once_per_utc_day_from_previous_day_volume(self):
def test_universe_failure_blocks_entries_without_fallback(self):
    # 종목 규칙 조회 실패 → UNIVERSE 설정, store "universe"는 비어 있음
def test_universe_below_min_count_blocks_entries(self):            # 통과 종목 4개
def test_day_rollover_resets_daily_baseline_and_appends_equity(self):
def test_restart_after_days_offline_rebuilds_day_state(self):      # Review Focus 3
    # risk_state.day가 4일 전 → 첫 run_once 뒤 day == 오늘, UNIVERSE 날짜 == 오늘, offline 초가 지표에 더해짐
def test_ledger_qty_missing_on_exchange_sets_recon_halt(self):
    # 장부 1.0, 거래소 0.5 → RECON_MISMATCH. 거래소 잔고를 1.0으로 되돌리면 다음 루프에서 해제
def test_recon_tolerates_less_than_one_step(self):
def test_unmanaged_assets_are_ignored(self):
    # 거래소에 장부에 없는 ETH 5개 → 중단 없음, equity 변화 없음, snapshot["unmanaged_assets"]에 "ETH"
def test_daily_loss_halts_until_next_utc_day(self):                # equity 960 → DAILY_LOSS
def test_drawdown_halt_clears_when_recovered(self):
def test_feed_unhealthy_sets_feed_stale(self):
def test_rate_limit_result_sets_halt_and_skips_calls_until_retry_after(self):
def test_fatal_result_sets_fatal_and_never_auto_clears(self):
def test_loop_exception_is_recorded_not_raised(self):
    # 거래소 메서드가 예외를 던져도 run_once는 ok=False인 LoopReport를 돌려준다
def test_state_survives_restart(self):
    # 새 Engine을 같은 store로 만들면 positions, peak_equity, halts가 그대로
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현** — 감시 종목 재선정은 `symbol_rules()` → `is_excluded` 통과 종목마다 `daily_candles(symbol, CANDLE_COUNT)` → `select_universe`. 받은 캔들은 그날의 캔들 캐시로 쓴다. 호출 사이에 `sleep(0.1)`. `RECON_MISMATCH`가 `RECON_ESCALATE_SECONDS` 넘게 이어지면 `snapshot`의 해당 중단에 `"needs_manual": True`를 단다. 피드 구독 대상은 감시 종목 + `MARKET_PROXY_SYMBOL` + 보유 종목이다.

- [ ] **Step 4: 통과 확인 후 커밋** — `git commit -m "feat: 엔진의 계좌·날짜·대조·중단 처리 추가"`

---

### Task 11: 엔진 — 청산, 보호 손절 주문, 주문 의도 복구

**Files:**
- Modify: `coin_sniper/engine.py`
- Create: `tests/coin/test_engine_exits.py`

**Interfaces:**
- Consumes: `update_stop`, `time_stop_due`, `floor_to_step`, `floor_to_tick`, `add_intent`, `new_client_id`
- Produces: `Engine` 내부 동작. 체결 기록(`trades`) 필드:
  `time, type`(`"BUY"|"SELL"`)`, symbol, qty, price, quote_qty, fees, strategy, reason, client_id`,
  매도에는 추가로 `entry_price, gross_pnl, net_pnl, net_return_pct, slippage_pct, counted`(통계 포함 여부, 수동 정리는 `False`).

주문 공통 절차(스펙 7.1): 의도 저장 → 전송 → `get_order`로 확인(`is_final`이 될 때까지 1초 간격, `ORDER_CONFIRM_TIMEOUT_SECONDS` 한도) → 장부 반영 → 의도 삭제. 전송이 `transient`로 실패해도 의도는 남기고 `get_order`로 확인한다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_stop_hit_sells_at_market_and_records_net_pnl(self):
    # 매수가 100, 손절 93, 현재가 92 → SELL 기록, reason "동적 손절", net_pnl은 실제 체결가와 수수료 기준
    # risk_state.realized_pnl이 net_pnl만큼 변함
def test_bot_exit_cancels_protective_order_before_selling(self):
    # ex.calls에서 cancel_order가 market_sell보다 먼저
def test_protective_cancel_failure_blocks_sell_and_sets_halt(self):
    # cancel_order 실패 → market_sell 호출 없음, PROTECTIVE_CANCEL 설정, 포지션 유지. 다음 루프에서 성공하면 해제
def test_time_stop_exits_after_168_hours(self):
def test_position_older_than_168h_exits_on_first_loop_after_restart(self):   # Review Focus 3
def test_protect_then_trail_stop_levels_are_persisted(self):
def test_protective_order_replaced_only_after_min_move(self):
    # 보호선 93 → 93.3 (0.5% 미만): 재등록 없음 / 93 → 101.5: cancel 후 place_stop
def test_protective_prices_and_qty_are_floored_to_tick_and_step(self):       # Review Focus 4
    # tick 0.01, 계산된 손절가 93.0187 → stop_price 93.01, limit_price == floor(93.01*0.99) to tick
def test_exchange_stop_filled_first_is_recorded_as_sell(self):
    # ex.trigger_stops() 후 루프 → SELL 기록 reason "거래소 손절", 포지션 삭제, market_sell 호출 없음
def test_missing_protective_order_is_replaced_with_risk_event(self):
def test_protective_stops_disabled_never_calls_place_stop(self):             # PROTECTIVE_STOPS_ENABLED False
def test_partial_sell_keeps_remainder_and_retries(self):
    # fill_ratio 0.4 → SELL 0.4 기록, 포지션 qty 0.6. 다음 루프에서 나머지 매도
def test_remainder_below_min_notional_becomes_dust(self):
    # 남은 금액 < min_notional → 포지션 닫힘, store "dust"에 기록
def test_unconfirmed_sell_keeps_position_and_sets_halt(self):
    # get_order가 계속 transient → ORDER_UNCONFIRMED, 포지션 유지, 의도 유지
def test_pending_intent_resolved_after_crash(self):
    # drop_response_next("market_sell") 후 새 Engine으로 startup+run_once → SELL 한 건만 기록, 의도 없음
def test_intent_for_order_exchange_never_saw_is_dropped(self):
    # get_order → exists False → 의도 삭제, 포지션 변화 없음
def test_exit_skipped_when_quote_missing(self):                              # 시세 없으면 청산 판단 보류 + RISK 이벤트
def test_untradable_symbol_rejection_does_not_spam(self):                    # Review Focus 2
    # ex.set_trading("XUSDT", False) + 손절 조건 → 5회 루프 뒤에도 예외 없음, RISK 이벤트 1건(count 5), 포지션 유지
def test_exits_run_even_when_entry_halted(self):                             # DAILY_LOSS 설정 상태에서도 손절 실행
def test_order_refused_during_maintenance_sets_halt_and_retries_later(self):
    # market_sell이 transient 실패 + get_order exists False → 의도 삭제, MAINTENANCE 설정, 포지션 유지
    # 다음 루프에서 계좌 조회가 성공하면 MAINTENANCE 해제 후 매도 재시도 → SELL 한 건
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현** — 진입 후 고점은 루프마다 `max(pos["highest_price"], quote.price)`로 갱신한다(피드의 당일 고가를 쓰지 않는다. 진입 전 고가가 섞이기 때문이다). 손절 판정은 `quote.price <= stop_price`. 매도 수량은 `floor_to_step(pos["qty"], step)`. 슬리피지 = `(주문 직전 시세 − 체결 평균가) / 주문 직전 시세`(매도 기준). 의도 해소는 `run_once`의 1단계에서 `kind`별로 같은 반영 함수를 쓴다.

- [ ] **Step 4: 통과 확인 후 커밋** — `git commit -m "feat: 엔진의 청산과 보호 손절 주문 추가"`

---

### Task 12: 엔진 — 진입

**Files:**
- Modify: `coin_sniper/engine.py`
- Create: `tests/coin/test_engine_entries.py`

**Interfaces:**
- Consumes: `evaluate_signals`, `confirm_persistent_signals`, `in_entry_blackout`, `day_progress`, `size_entry`, `position_open_risk`, `account_value`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_signal_needs_three_consecutive_loops(self):
    # 1, 2번째 루프: 주문 없음 / 3번째: BUY
def test_buy_records_net_qty_after_base_fee(self):
    # 체결 1.0, 수수료 0.001(받은 코인) → 포지션 qty 0.999, cost == quote_qty
def test_position_fields_initialized(self):
    # buy_price == 체결 평균가, stop_price == buy_price*(1-stop_loss_pct), highest_price == buy_price, entry_time == now
def test_protective_stop_placed_right_after_buy(self):
def test_protective_place_failure_keeps_position_and_logs_risk(self):
    # place_stop 실패 → 포지션은 기록, RISK 이벤트, protective None (다음 루프에서 Task 11의 재등록이 처리)
def test_entry_uses_live_quote_price_for_sizing(self):
def test_stale_quote_skips_with_event(self):
def test_no_entry_during_blackout(self):                       # UTC 00:10
def test_no_entry_when_any_halt_active(self):
def test_no_entry_when_btc_below_falling_ma(self):
def test_max_positions_respected(self):
def test_already_held_symbol_skipped(self):
def test_third_position_shrinks_to_remaining_open_risk(self):
    # 0.7% 위험 포지션 둘 보유 → 세 번째 계획 손실 ≈ equity*0.006
def test_below_min_notional_skips_with_event(self):
def test_partial_buy_records_filled_part_only(self):
def test_zero_fill_does_not_create_position(self):
def test_unconfirmed_buy_sets_halt_and_stops_further_entries(self):
def test_cash_limited_by_actual_free_usdt(self):
    # 봇 현금 1000, 거래소 USDT 100 → 주문 금액 ≤ 100, RISK 이벤트
def test_drawdown_multiplier_halves_size(self):
def test_report_counts_raw_and_confirmed_signals(self):
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현** — 신호는 `score` 오름차순(원본과 동일). 한 루프에서 여러 종목을 살 수 있고, 살 때마다 `cash`와 `open_risk_total`을 갱신한다. 매수 수량은 `size_entry`의 결과를 그대로 `market_buy`에 넘긴다. 건너뛴 사유는 `SKIP` 이벤트로 남긴다.

- [ ] **Step 4: 전체 테스트 통과 확인 후 커밋** — `git commit -m "feat: 엔진의 진입 처리 추가"`

---

### Task 13: 대시보드

**Files:**
- Create: `coin_sniper/dashboard.py`, `tests/coin/test_dashboard.py`
- Modify: `coin_sniper/engine.py` (`snapshot` 완성)

**Interfaces:**
- Consumes: `Engine.snapshot`, `Metrics.summary`, `period_returns`
- Produces:

```python
def performance_stats(trades: list[dict]) -> dict
def render_dashboard(snapshot: dict) -> str
def build_state_json(snapshot: dict) -> dict
def publish(readme: str, state: dict, events: list[dict], repo_dir: Path, work_dir: Path) -> bool
class PublishSchedule:
    def __init__(self); def mark_dirty(self, critical: bool = False) -> None
    def due(self, now_ts: float, has_positions: bool) -> bool; def published(self, now_ts: float) -> None
```

`snapshot` 키: `mode, now, status`(`"running"|"halted"|"error"`)`, entry_allowed, halts, feed_connected, feed_error, last_quote_at, last_account_sync, last_loop_at, commit, universe_date, universe`(각 `{symbol, price, max_qty, eligible, reason}`)`, signal_progress, equity, cash, drawdown_pct, daily_return_pct, size_multiplier, open_risk_pct, free_slots, positions`(각 `{symbol, qty, buy_price, price, return_pct, initial_stop, stop_price, protective`(`"on"|"off"|"disabled"`)`, hours_left, trailing_active}`)`, unmanaged_assets, protective_stops_enabled, trades, events, metrics`(`{"24h","7d","all"}` 각각 `summary`)`, returns`(`period_returns` 결과)`, weekly_equity`.

`performance_stats` 키: `closed, win_rate, avg_win_pct, avg_loss_pct, profit_factor, max_loss_streak, total_net_pnl, total_fees_usdt, avg_slippage_pct, max_drawdown_pct, by_strategy`. `counted`가 `False`인 매도는 뺀다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_mode_banner_on_first_heading(self):                 # "[DEMO]"가 첫 줄에
def test_all_spec_sections_present(self):
    # "현재 상태", "시스템 신선도", "오늘의 감시", "위험 관리", "보유 포지션", "수익률", "오류율", "최근 7일 이벤트"
def test_times_rendered_in_kst(self):                        # UTC 10:18 → "19:18"
def test_returns_table_has_btc_comparison(self):
def test_error_rate_table_has_three_windows(self):
def test_needs_manual_halt_shows_manual_cleanup_label(self): # "수동 정리 필요"
def test_bot_only_stop_warning_when_protective_disabled(self):
def test_performance_stats_excludes_uncounted_trades(self):
def test_performance_stats_values(self):
    # 순수익률 +10, -5, +2 → win_rate 66.7, max_loss_streak 1, profit_factor 계산값
def test_no_secrets_in_output(self):                         # 렌더 결과와 state JSON에 키·시크릿 문자열 없음
def test_markdown_pipe_in_reason_is_escaped(self):
def test_schedule_critical_is_immediate_dirty_waits_5min_heartbeat_30min(self):
def test_publish_failure_returns_false_without_raising(self):
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현** — `publish`는 원본 `sync_dashboard_to_branch`의 git 절차(`hash-object -w` → `mktree` → `commit-tree` → `push -f origin <commit>:refs/heads/dashboard`)를 옮긴다. 파일은 `work_dir`에 쓰고 트리에는 `README.md`, `dashboard_state.json`, `dashboard_events.json` 이름으로 넣는다. 커밋 메시지는 `dashboard: rolling 7-day snapshot`. 대시보드 배치는 스펙 8절의 순서를 따른다.

- [ ] **Step 4: 통과 확인 후 커밋** — `git commit -m "feat: 수익률·오류율 대시보드 추가"`

---

### Task 14: 진입점, 수동 정리, 문서, 데모 가동

**Files:**
- Create: `coin_bot.py`, `tests/coin/test_cli.py`, `docs/coin-sniper.md`
- Modify: `README.md` (맨 위 소개 아래에 `docs/coin-sniper.md` 링크 한 줄)

**Interfaces:**
- Consumes: 전부
- Produces: `python coin_bot.py run`, `python coin_bot.py reconcile [--reset-capital]`

```python
def reconcile(store: StateStore, exchange: Exchange, now: datetime, reset_capital: bool) -> list[str]   # 정리한 심볼
def main(argv: list[str]) -> int
```

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_reconcile_closes_positions_missing_on_exchange_as_uncounted(self):
    # 장부 X 1.0, 거래소 X 0 → 포지션 삭제, trades에 type "SELL", reason "수동 정리", counted False, net_pnl 0
    # risk_state.segment_start == now
def test_reconcile_keeps_positions_that_match(self):
def test_reconcile_reset_capital_restores_starting_capital(self):
    # realized_pnl 0, peak_equity == starting_capital
def test_reconcile_aborts_when_balances_unavailable(self):       # 장부를 건드리지 않는다
def test_run_refuses_without_keys(self):                         # 종료 코드 2
def test_run_refuses_on_corrupt_state(self):                     # 종료 코드 2, 원인 파일 이름 출력
def test_run_refuses_when_already_running(self):
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현** — `run`: `.env` 로드 → `load_settings` → `require_keys` → 잠금 → `Metrics`/`StateStore`/`BinanceExchange`/`BinanceFeed` 생성(`on_call`은 현재 UTC 시각을 붙여 `metrics.record_api(endpoint, result, now)`를 부르는 함수, `on_reconnect`는 같은 방식으로 `metrics.record_feed_reconnect`) → `engine.startup` → `POLL_INTERVAL_SECONDS` 간격으로 `run_once`, 루프마다 `PublishSchedule`에 따라 `publish`(`AUTO_GIT_PUSH`가 참일 때만). 게시 실패는 매매를 멈추지 않는다. `reconcile`은 봇이 떠 있으면(잠금 실패) 거부한다.

- [ ] **Step 4: `docs/coin-sniper.md` 작성** — 설치, `.env` 항목, 0단계 확인, 실행, 수동 정리, 대시보드, 데모 잔고 초기화 시 할 일, 한계(데모 시세는 실거래소와 동일하지 않음, 수치는 미검증 출발값).

- [ ] **Step 5: 전체 테스트** — Run: `python -m unittest discover -s tests/coin -v` / Expected: 전부 OK. 원본 테스트도 그대로인지 확인: `python -m unittest discover -s tests -p "test_*.py" -v`(리눅스에서만 통과한다. 윈도우에서는 원본이 `fcntl`을 import해서 실패하며 이는 변경 전과 같다).

- [ ] **Step 6: 데모 가동 확인** — Run: `python coin_bot.py run`을 10분간 실행.

Expected: 콘솔에 루프 완료 로그가 30초마다, `state/demo/`에 `risk_state.json`, `universe.json`, `metrics.json` 생성, `universe.json`에 종목 5개 이상, 예외 추적 출력 없음. `AUTO_GIT_PUSH=true`로 한 번 더 돌려 `origin/dashboard`에 `[DEMO]` 대시보드가 올라가는지 확인한다(공개 저장소면 보유·체결 내역이 공개된다는 점을 사용자에게 먼저 알리고 동의를 받는다).

- [ ] **Step 7: 커밋** — `git commit -m "feat: 코인 봇 실행 명령과 수동 정리 추가"`
