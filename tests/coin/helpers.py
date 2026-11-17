"""엔진 테스트 공용 도우미. 네트워크·실제 sleep 없이 가짜 거래소와 피드로 엔진을 만든다."""
from __future__ import annotations

import shutil
import tempfile
import unittest
import weakref
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from coin_sniper.config import Settings
from coin_sniper.engine import Engine
from coin_sniper.exchange.fake import FakeExchange, FakeFeed
from coin_sniper.ledger import StateStore
from coin_sniper.metrics import Metrics
from coin_sniper.models import SymbolRules

TODAY = date(2026, 11, 17)          # FakeFeed.set_quote의 기본 day와 같다
DEFAULT_PRICES = {
    "BTCUSDT": 90000.0, "ETHUSDT": 3000.0, "SOLUSDT": 100.0, "XRPUSDT": 2.0,
    "ADAUSDT": 0.5, "DOGEUSDT": 0.2, "LINKUSDT": 15.0,
}


def utc(y: int, m: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, s, tzinfo=timezone.utc)


def rule(symbol: str, step: float = 0.001) -> SymbolRules:
    return SymbolRules(symbol=symbol, base=symbol[:-4], quote="USDT", trading=True,
                       step_size=step, tick_size=0.0001, min_notional=5.0)


def trending_candles(n: int, start: float, step: float, today: date = TODAY,
                     quote_vol: float = 5e7) -> list[dict]:
    """과거→현재 n개 일봉. 마지막 봉의 날짜가 today(진행 중인 봉)."""
    out = []
    for i in range(n):
        close = start + step * i
        out.append({"date": (today - timedelta(days=n - 1 - i)).isoformat(),
                    "open": close - step / 2, "high": close * 1.01, "low": (close - step / 2) * 0.99,
                    "close": close, "vol": 1000.0, "quote_vol": quote_vol})
    return out


def position(symbol: str, qty: float, buy_price: float) -> dict:
    return {"symbol": symbol, "qty": qty, "buy_price": buy_price, "cost": qty * buy_price,
            "stop_loss_pct": 0.05, "trailing_gap_pct": 0.05, "highest_price": buy_price,
            "stop_price": buy_price * 0.95, "trailing_active": False,
            "entry_time": "2026-11-16T12:00:00+00:00", "strategy": "vbo", "protective": None}


def make_settings(state_dir: Path, starting_capital: float = 1000.0) -> Settings:
    return Settings(mode="demo", rest_base="", stream_base="", api_key="", api_secret="",
                    state_dir=state_dir, starting_capital=starting_capital, auto_git_push=False)


def make_engine(prices: dict[str, float] | None = None, balances: dict[str, float] | None = None,
                starting_capital: float = 1000.0, today: date = TODAY,
                state_dir: Path | None = None, test: unittest.TestCase | None = None,
                rule_overrides: dict[str, SymbolRules] | None = None,
                ) -> tuple[Engine, FakeExchange, FakeFeed, StateStore]:
    """기본값: 7개 통과 종목(BTCUSDT 포함, 일봉 70개), 가격·신선한 시세, USDT 10000.
    startup은 부르지 않는다. 직접 만든 임시 상태 폴더는 test.addCleanup(없으면 엔진 소멸 시)으로 지운다."""
    prices = dict(DEFAULT_PRICES if prices is None else prices)
    if state_dir is None:
        state_dir = Path(tempfile.mkdtemp(prefix="coin-engine-"))
        owned = True
    else:
        owned = False
    rules = {s: rule(s, 0.00001 if s == "BTCUSDT" else 0.001) for s in prices}
    rules.update(rule_overrides or {})
    ex = FakeExchange(rules, {"USDT": 10000.0} if balances is None else balances)
    feed = FakeFeed()
    for symbol, price in prices.items():
        ex.set_price(symbol, price)
        ex.set_candles(symbol, trending_candles(70, price * 0.8, price * 0.003, today))
        feed.set_quote(symbol, price, day=today.isoformat())
    store = StateStore(state_dir)
    engine = Engine(ex, feed, store, Metrics(), make_settings(state_dir, starting_capital),
                    sleep=lambda _s: None, monotonic=lambda: 0.0)   # 시간이 흐르지 않는 가짜 단조 시계
    if owned:
        if test is not None:
            test.addCleanup(shutil.rmtree, state_dir, True)
        else:
            weakref.finalize(engine, shutil.rmtree, state_dir, True)
    return engine, ex, feed, store


def add_position(engine: Engine, exchange: FakeExchange, symbol: str, qty: float, buy_price: float,
                 **fields) -> dict:
    """startup 뒤에 부른다: 장부에 포지션을 넣어 저장하고, 가짜 거래소에 같은 수량의 기초 자산을 넣는다."""
    pos = position(symbol, qty, buy_price)
    pos.update(fields)
    engine.positions[symbol] = pos
    engine.store.save("positions", engine.positions)
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    exchange._account(base)[0] += Decimal(repr(float(qty)))
    return pos


def restart_engine(engine: Engine, now: datetime, sleep=None) -> Engine:
    """같은 상태 폴더·거래소·피드로 새 엔진을 만들어 startup까지 한다(프로세스 재시작 흉내)."""
    fresh = Engine(engine.exchange, engine.feed, StateStore(engine.store.state_dir), Metrics(),
                   engine.settings, sleep=sleep or (lambda _s: None), monotonic=engine.monotonic)
    fresh.startup(now)
    return fresh


SIGNAL_VOLUME = 2000.0              # 직전 20일 평균(1000)의 2배: 하루 중 언제든 거래량 확인을 통과한다


def signal_candles(price: float, today: date = TODAY, open_ratio: float = 0.95, n: int = 70) -> list[dict]:
    """변동성 돌파(VBO) 신호가 나는 일봉. 완료봉은 완만히 상승(20일선 상승), 당일봉은 시가 price×open_ratio에서
    돌파 목표가를 넘어 price에 있다. open_ratio가 낮을수록 돌파폭(score)이 크다. 손절폭은 0.04로 정해진다."""
    out = []
    for i in range(n - 1):
        close = price * (0.85 + 0.001 * i)
        out.append({"date": (today - timedelta(days=n - 1 - i)).isoformat(), "open": close * 0.999,
                    "high": close * 1.002, "low": close * 0.998, "close": close, "vol": 1000.0,
                    "quote_vol": 5e7})
    o, h, lo = price * open_ratio, price * 1.001, price * open_ratio * 0.999
    out.append({"date": today.isoformat(), "open": o, "high": h, "low": lo, "close": price,
                "vol": SIGNAL_VOLUME, "quote_vol": SIGNAL_VOLUME * price})
    return out


def set_signal(exchange: FakeExchange, feed: FakeFeed, symbol: str, price: float | None = None,
               today: date = TODAY, open_ratio: float = 0.95, age: float = 0.0) -> None:
    """symbol에 진짜 VBO 신호를 만든다(REST 일봉 + 같은 값의 실시간 당일봉 시세). 첫 run_once 전에 부른다
    (감시 종목 선정 때 일봉이 캐시된다). 기본 BTCUSDT 일봉(상승 추세)이 시장 필터를 통과한다."""
    price = DEFAULT_PRICES[symbol] if price is None else price
    candles = signal_candles(price, today, open_ratio)
    exchange.set_candles(symbol, candles)
    exchange.set_price(symbol, price)
    last = candles[-1]
    feed.set_quote(symbol, price, open=last["open"], high=last["high"], low=last["low"],
                   volume=SIGNAL_VOLUME, day=today.isoformat(), age=age)
