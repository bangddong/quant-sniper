"""바이낸스 일봉 시세 피드. 백그라운드 스레드의 WebSocket이 심볼별 최신 일봉 시세를 보관한다.

구조는 루트 kiwoom_realtime.py(스레드 + asyncio 루프 + 락으로 보호되는 시세 dict + 재접속 루프)를 따른다.
접속 주소는 오직 settings.stream_base이며, 스트림은 인증이 없다.
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Iterable

from coin_sniper.config import FEED_RECONNECT_DELAY_SECONDS, QUOTE_MAX_AGE_SECONDS
from coin_sniper.models import Quote

_SYNC_INTERVAL_SECONDS = 1.0


def _stream_name(symbol: str) -> str:
    return f"{symbol.lower()}@kline_1d"


def subscription_requests(previous: set[str], current: set[str], next_id: int) -> list[dict]:
    """previous → current 로 바꾸는 데 필요한 UNSUBSCRIBE/SUBSCRIBE 요청(params는 정렬)."""
    requests: list[dict] = []
    removed = sorted(_stream_name(s) for s in previous - current)
    added = sorted(_stream_name(s) for s in current - previous)
    if removed:
        requests.append({"method": "UNSUBSCRIBE", "params": removed, "id": next_id})
        next_id += 1
    if added:
        requests.append({"method": "SUBSCRIBE", "params": added, "id": next_id})
    return requests


class BinanceFeed:
    def __init__(self, settings, on_reconnect: Callable[[], None] | None = None, clock=time.time):
        self._url = settings.stream_base
        self._on_reconnect = on_reconnect
        self._clock = clock
        self._lock = threading.Lock()
        self._symbols: set[str] = set()
        self._quotes: dict[str, Quote] = {}
        self._connected = False
        self._last_error = ""
        self._reconnects = 0
        self._attempts = 0
        self._thread: threading.Thread | None = None

    # ---- Feed 프로토콜 ----------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="binance-feed", daemon=True)
            thread = self._thread
        thread.start()

    def set_symbols(self, symbols: Iterable[str]) -> None:
        wanted = {str(s).upper() for s in symbols}
        with self._lock:
            self._symbols = wanted
            for symbol in set(self._quotes) - wanted:
                del self._quotes[symbol]

    def quote(self, symbol: str, max_age: float = QUOTE_MAX_AGE_SECONDS) -> Quote | None:
        with self._lock:
            q = self._quotes.get(symbol)
            if q is None or self._clock() - q.received_at > max_age:
                return None
            return replace(q)

    def health(self) -> tuple[bool, str]:
        with self._lock:
            return self._connected, self._last_error

    def take_reconnect_count(self) -> int:
        with self._lock:
            count, self._reconnects = self._reconnects, 0
            return count

    # ---- 메시지 처리 ------------------------------------------------------------
    def handle_message(self, raw) -> None:
        """kline 이벤트만 시세로 반영한다. 그 밖의 모든 입력은 조용히 무시한다."""
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict) or msg.get("e") != "kline":
                return
            k = msg["k"]
            symbol = str(msg["s"]).upper()
            day = datetime.fromtimestamp(int(k["t"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            quote = Quote(symbol=symbol, price=float(k["c"]), open=float(k["o"]),
                          high=float(k["h"]), low=float(k["l"]), volume=float(k["v"]),
                          quote_volume=float(k["q"]), day=day, received_at=0.0)
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            return
        prices = (quote.open, quote.high, quote.low, quote.price)
        if not all(math.isfinite(p) and p > 0 for p in prices):   # 0·음수·nan·inf 가격은 시세가 아니다
            return
        with self._lock:
            quote.received_at = self._clock()
            self._quotes[symbol] = quote

    # ---- 내부 상태 ----------------------------------------------------------------
    def _symbols_snapshot(self) -> set[str]:
        with self._lock:
            return set(self._symbols)

    def _set_health(self, connected: bool, error: BaseException | None = None) -> None:
        with self._lock:
            self._connected = connected
            if error is not None:
                self._last_error = f"{type(error).__name__}: {error}"

    def _note_attempt(self) -> None:
        """접속 시도를 센다. 첫 시도가 아니면 재접속이다."""
        with self._lock:
            self._attempts += 1
            reconnect = self._attempts > 1
            if reconnect:
                self._reconnects += 1
        if reconnect and self._on_reconnect is not None:
            try:
                self._on_reconnect()             # 락 밖에서 호출한다
            except Exception:
                pass

    # ---- 스레드 / 소켓 루프 (테스트가 실행하지 않는다) -----------------------------
    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        while True:
            self._note_attempt()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_health(False, exc)
            else:
                self._set_health(False)
            await asyncio.sleep(FEED_RECONNECT_DELAY_SECONDS)

    async def _session(self) -> None:
        import websockets

        async with websockets.connect(self._url) as ws:
            self._set_health(True)
            request_id = 1
            subscribed: set[str] = set()
            while True:
                current = self._symbols_snapshot()
                for req in subscription_requests(subscribed, current, request_id):
                    await ws.send(json.dumps(req))
                    request_id = req["id"] + 1
                subscribed = current
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=_SYNC_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    continue
                self.handle_message(raw)
