import os

# 동적 스크리닝 활성화 여부
DYNAMIC_SCREENING = True
SCREENER_TOP_N = 20
MIN_MARKET_CAP_KRW = 1_000_000_000_000 # 🛡️ 시가총액 최소 1조 원 이상 (하루짜리 잡주/테마주 원천 차단)
MAX_MARKET_CAP_KRW = 5_000_000_000_000 # 🛡️ 시가총액 최대 5조 원 이하 (굼뜬 10조+ 메가캡 배제, 탄력적 황금구간)

# 고정 코어 유니버스 (동적 스크리닝 비활성화 시 또는 오프라인 폴백용)
DEFAULT_UNIVERSE = {
    "삼성전자": "005930",
    "SK하이닉스": "000660",
    "현대차": "005380",
    "기아": "000270",
    "NAVER": "035420",
    "카카오": "035720",
    "셀트리온": "068270",
    "신한지주": "055550",
    "KB금융": "105560",
    "삼성바이오로직스": "207940",
    "LG에너지솔루션": "373220",
    "POSCO홀딩스": "005490",
    "KODEX200": "069500",
    "KODEX레버리지": "122630",
    "KODEX코스닥150": "229200",
    "TIGER미국S&P500": "360750",
    "TIGER미국나스닥100": "133690"
}

# 전략 파라미터
RSI_PERIOD = 14
RSI_BUY_THRESHOLD = 28.0      # RSI 28 이하 시 과매도 저격 매수
VBO_K = 0.5                   # 변동성 돌파 계수 (전일 레인지의 0.5)

# 청산 및 리스크 관리 규칙
STOP_LOSS_PCT = 0.020         # -2.0% 기계적 칼손절
TRAILING_STOP_TRIGGER = 0.040 # +4.0% 수익 시 트레일링 스탑 발동
TRAILING_STOP_LOCK = 0.010    # 발동 후 손절선을 매수가 +1.0%로 상향
TAKE_PROFIT_PCT = 0.080       # +8.0% 목표 익절
MAX_HOLD_DAYS = 7             # 최대 보유일 (7거래일 경과 시 타임스탑)

# 자금 관리 규칙
MAX_ALLOC_PER_STOCK = 0.20    # 1종목당 계좌 총자산의 최대 20%
MAX_POSITIONS = 4             # 최대 동시 보유 종목 수 (최소 20% 현금 버퍼 유지)
DAILY_KILL_SWITCH_PCT = 0.040 # 일일 계좌 누적 손실 -4.0% 도달 시 당일 올스톱

# 감시 및 호출 주기
POLL_INTERVAL_SECONDS = 60    # 장중 1분 주기 감시
API_REQUEST_DELAY = 0.3       # API 연속 호출 시 0.3초 대기 (레이트 리밋 방어)

# 키움 API 엔드포인트
MOCK_API_BASE_URL = "https://openapivts.koreainvestment.com:29443"
REAL_API_BASE_URL = "https://openapi.koreainvestment.com:9443"
