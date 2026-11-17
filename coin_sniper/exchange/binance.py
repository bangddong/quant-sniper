"""바이낸스 현물 REST 어댑터. 거래소와 HTTP로 대화하는 유일한 모듈.

- 모든 공개 메서드는 ApiResult를 돌려주며 네트워크/HTTP/JSON 문제로 예외를 던지지 않는다.
- 스스로 재시도하지 않는다. 예외는 시계 오차(-1021) 때 시간을 맞추고 딱 한 번 다시 보내는 것뿐이다.
- 키/시크릿/서명은 로그·오류 문자열·on_call 인자에 남기지 않는다.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from urllib.parse import urlencode

from coin_sniper.config import (DEFAULT_BAN_RETRY_SECONDS, HTTP_TIMEOUT_SECONDS, QUOTE_ASSET,
                                RECV_WINDOW_MS, Settings)
from coin_sniper.models import ApiResult, OrderState, SymbolRules

_FATAL_CODES = {-2014, -2015}
_TRANSIENT_CODES = {-1003, -1001, -1007, -1008}
_CLOCK_CODE = -1021
_UNKNOWN_ORDER = -2013
_STATUS_MAP = {"PENDING_CANCEL": "NEW", "EXPIRED_IN_MATCH": "EXPIRED"}


def _fmt(value: float) -> str:
    """지수 표기 없는 십진 문자열."""
    text = format(Decimal(repr(float(value))), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _fail(kind: str, error: str, retry_after: float = 0.0) -> ApiResult:
    return ApiResult(ok=False, value=None, kind=kind, error=error, retry_after=retry_after)


class BinanceExchange:
    def __init__(self, settings: Settings, on_call: Callable[[str, ApiResult], None] | None = None,
                 opener: Callable = urllib.request.urlopen, clock: Callable[[], float] = time.time):
        self._settings = settings
        self._on_call = on_call
        self._opener = opener
        self._clock = clock
        self._offset_ms = 0

    # ---- 저수준 ----------------------------------------------------------------
    def _scrub(self, text: str) -> str:
        for secret in (self._settings.api_secret, self._settings.api_key):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _sign(self, query: str) -> str:
        return hmac.new(self._settings.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _report(self, name: str, result: ApiResult) -> ApiResult:
        if self._on_call is not None:
            self._on_call(name, result)
        return result

    def _send(self, name: str, method: str, path: str, params: dict,
              signed: bool) -> tuple[ApiResult, int | None]:
        """HTTP 한 번. on_call을 정확히 한 번 부른다. (결과, 거래소 오류 코드)를 돌려준다."""
        try:
            result, code = self._send_once(method, path, params, signed)
        except Exception as exc:     # 공개 메서드는 절대 예외를 던지지 않는다. 메시지는 키 노출 방지를 위해 버린다.
            result, code = self._unexpected(exc, signed), None
        return self._report(name, result), code

    @staticmethod
    def _unexpected(exc: Exception, signed: bool) -> ApiResult:
        name = type(exc).__name__
        header_problem = signed and (isinstance(exc, UnicodeEncodeError)
                                     or (isinstance(exc, ValueError) and "header" in str(exc).lower()))
        if header_problem:      # API 키 값 자체가 헤더로 보낼 수 없는 형식: 로컬 설정 문제
            return _fail("fatal", f"invalid API key format for request header ({name})")
        return _fail("transient", f"unexpected error: {name}")

    def _send_once(self, method: str, path: str, params: dict,
                   signed: bool) -> tuple[ApiResult, int | None]:
        params = dict(params)
        if signed:
            params["timestamp"] = int(self._clock() * 1000 + self._offset_ms)
            params["recvWindow"] = RECV_WINDOW_MS
        query = urlencode(params)
        if signed:
            query += "&signature=" + self._sign(query)
        url = self._settings.rest_base + path + ("?" + query if query else "")
        headers = {"X-MBX-APIKEY": self._settings.api_key} if signed else {}
        request = urllib.request.Request(url, data=b"" if method == "POST" else None,
                                         headers=headers, method=method)
        try:
            with self._opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except Exception:
                body = b""
            finally:
                exc.close()
            return self._classify_http(exc.code, body, exc.headers)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            return _fail("transient", self._scrub(f"network error: {type(exc).__name__}: {reason}")), None
        try:
            return ApiResult(ok=True, value=json.loads(raw.decode("utf-8"))), None
        except (ValueError, UnicodeDecodeError):
            note = "; outcome unknown, reconcile via get_order" if method in ("POST", "DELETE") else ""
            return _fail("transient", "invalid JSON in response" + note), None

    def _classify_http(self, status: int, body: bytes, headers) -> tuple[ApiResult, int | None]:
        code, msg = None, ""
        try:
            payload = json.loads(body.decode("utf-8"))
            if isinstance(payload, dict):
                code = payload.get("code")
                msg = str(payload.get("msg", ""))
        except (ValueError, UnicodeDecodeError):
            msg = body[:200].decode("utf-8", "replace")
        if not isinstance(code, int):
            code = None
        retry_after = 0.0
        if status in (418, 429):
            try:
                retry_after = float(headers.get("Retry-After")) if headers is not None else 0.0
            except (TypeError, ValueError):
                retry_after = 0.0
            if status == 418 and retry_after <= 0:
                retry_after = float(DEFAULT_BAN_RETRY_SECONDS)
        error = self._scrub(f"HTTP {status}" + (f" code {code}" if code is not None else "")
                            + (f": {msg}" if msg else ""))
        if code in _FATAL_CODES or status in (401, 403):
            kind = "fatal"
        elif code == _CLOCK_CODE or code in _TRANSIENT_CODES or status >= 500 or status in (418, 429):
            kind = "transient"
        else:
            kind = "permanent"
        return _fail(kind, error, retry_after), code

    def _call(self, name: str, method: str, path: str, params: dict | None = None,
              signed: bool = True) -> ApiResult:
        return self._call_with_code(name, method, path, params, signed)[0]

    def _call_with_code(self, name: str, method: str, path: str, params: dict | None = None,
                        signed: bool = True) -> tuple[ApiResult, int | None]:
        """서명 요청 + (-1021일 때만) 시간 재동기화 후 1회 재시도."""
        params = params or {}
        result, code = self._send(name, method, path, params, signed)
        if not result.ok and signed and code == _CLOCK_CODE:
            sync = self.server_time()
            if sync.ok:
                self._offset_ms = sync.value - int(self._clock() * 1000)
                result, code = self._send(name, method, path, params, signed)
        return result, code

    # ---- 공개 API ---------------------------------------------------------------
    def server_time(self) -> ApiResult:
        res, _ = self._send("time", "GET", "/api/v3/time", {}, False)
        return self._parse(res, lambda d: int(d["serverTime"]))

    def symbol_rules(self) -> ApiResult:
        res = self._call("exchangeInfo", "GET", "/api/v3/exchangeInfo", signed=False)
        return self._parse(res, self._rules_from_info)

    def daily_candles(self, symbol: str, limit: int) -> ApiResult:
        res = self._call("klines", "GET", "/api/v3/klines",
                         {"symbol": symbol, "interval": "1d", "limit": int(limit)}, signed=False)
        return self._parse(res, self._candles_from_rows)

    def balances(self) -> ApiResult:
        res = self._call("account", "GET", "/api/v3/account")
        return self._parse(res, lambda d: {b["asset"]: (float(b["free"]), float(b["locked"]))
                                           for b in d["balances"]})

    def market_buy(self, symbol: str, qty: float, client_id: str) -> ApiResult:
        return self._market(symbol, "BUY", qty, client_id)

    def market_sell(self, symbol: str, qty: float, client_id: str) -> ApiResult:
        return self._market(symbol, "SELL", qty, client_id)

    def place_stop(self, symbol: str, qty: float, stop_price: float, limit_price: float,
                   client_id: str) -> ApiResult:
        params = {"symbol": symbol, "side": "SELL", "type": "STOP_LOSS_LIMIT", "timeInForce": "GTC",
                  "quantity": _fmt(qty), "stopPrice": _fmt(stop_price), "price": _fmt(limit_price),
                  "newClientOrderId": client_id, "newOrderRespType": "FULL"}
        return self._parse(self._call("order.post", "POST", "/api/v3/order", params), self._order_from,
                           mutating=True)

    def cancel_order(self, symbol: str, client_id: str) -> ApiResult:
        res = self._call("order.delete", "DELETE", "/api/v3/order",
                         {"symbol": symbol, "origClientOrderId": client_id})
        return self._parse(res, self._order_from, mutating=True)

    def get_order(self, symbol: str, client_id: str) -> ApiResult:
        res, code = self._call_with_code("order.get", "GET", "/api/v3/order",
                                         {"symbol": symbol, "origClientOrderId": client_id})
        if not res.ok and code == _UNKNOWN_ORDER:
            return ApiResult(ok=True, value=OrderState(client_id=client_id, symbol=symbol, side="",
                                                       status="UNKNOWN", exists=False))
        parsed = self._parse(res, lambda d: (self._order_from(d), d["orderId"]))
        if not parsed.ok:
            return parsed
        state, order_id = parsed.value
        if state.executed_qty > 0:
            trades = self._call("myTrades", "GET", "/api/v3/myTrades",
                                {"symbol": symbol, "orderId": order_id})
            fees = self._parse(trades, self._fees_from_trades)
            if not fees.ok:
                return fees
            state.fees = fees.value
        return ApiResult(ok=True, value=state)

    def open_orders(self, symbol: str | None = None) -> ApiResult:
        res = self._call("openOrders", "GET", "/api/v3/openOrders", {"symbol": symbol} if symbol else {})
        return self._parse(res, lambda rows: [self._order_from(r) for r in rows])

    # ---- 변환 -------------------------------------------------------------------
    def _market(self, symbol: str, side: str, qty: float, client_id: str) -> ApiResult:
        params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": _fmt(qty),
                  "newClientOrderId": client_id, "newOrderRespType": "FULL"}
        return self._parse(self._call("order.post", "POST", "/api/v3/order", params), self._order_from,
                           mutating=True)

    @staticmethod
    def _parse(res: ApiResult, fn: Callable, mutating: bool = False) -> ApiResult:
        """성공 응답을 변환한다. 모양이 예상과 다르면 0/빈 값으로 덮지 않고 실패로 돌려준다."""
        if not res.ok:
            return res
        try:
            return ApiResult(ok=True, value=fn(res.value))
        except (KeyError, TypeError, ValueError, ArithmeticError, IndexError, AttributeError) as exc:
            if mutating:    # 주문이 거래소에 있을 수 있다: 거절로 오인하지 않도록 결과 불명으로 알린다
                return _fail("transient", f"order response unparseable ({type(exc).__name__}); "
                                          "outcome unknown, reconcile via get_order")
            return _fail("permanent", f"unexpected response shape: {type(exc).__name__}")

    @staticmethod
    def _order_from(data: dict) -> OrderState:
        status = str(data["status"])
        fees: dict[str, float] = {}
        for fill in data.get("fills") or []:
            asset = fill["commissionAsset"]
            fees[asset] = fees.get(asset, 0.0) + float(fill["commission"])
        return OrderState(
            client_id=data.get("origClientOrderId") or data["clientOrderId"],
            symbol=data["symbol"], side=data["side"],
            status=_STATUS_MAP.get(status, status),
            executed_qty=float(data["executedQty"]),
            quote_qty=float(data["cummulativeQuoteQty"]),
            fees=fees)

    @staticmethod
    def _fees_from_trades(rows: list) -> dict[str, float]:
        fees: dict[str, float] = {}
        for row in rows:
            asset = row["commissionAsset"]
            fees[asset] = fees.get(asset, 0.0) + float(row["commission"])
        return fees

    @staticmethod
    def _candles_from_rows(rows: list) -> list[dict]:
        out = []
        for row in rows:
            day = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            out.append({"date": day, "open": float(row[1]), "high": float(row[2]), "low": float(row[3]),
                        "close": float(row[4]), "vol": float(row[5]), "quote_vol": float(row[7])})
        return out

    @staticmethod
    def _rules_from_info(info: dict) -> dict[str, SymbolRules]:
        rules: dict[str, SymbolRules] = {}
        for sym in info["symbols"]:
            if sym.get("quoteAsset") != QUOTE_ASSET:
                continue
            filters = {f["filterType"]: f for f in sym.get("filters", [])}
            notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
            if "LOT_SIZE" not in filters or "PRICE_FILTER" not in filters or notional is None:
                continue        # 규칙을 확인할 수 없는 종목은 거래 대상에서 뺀다(0으로 채우지 않는다)
            rules[sym["symbol"]] = SymbolRules(
                symbol=sym["symbol"], base=sym["baseAsset"], quote=sym["quoteAsset"],
                trading=sym.get("status") == "TRADING",
                step_size=float(filters["LOT_SIZE"]["stepSize"]),
                tick_size=float(filters["PRICE_FILTER"]["tickSize"]),
                min_notional=float(notional["minNotional"]))
        return rules
