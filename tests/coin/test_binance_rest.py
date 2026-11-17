import io
import json
import re
import unittest
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from coin_sniper.config import Settings
from coin_sniper.exchange.binance import BinanceExchange
from coin_sniper.models import OrderState

SECRET = "s3cr3t-value-xyz"
KEY = "key-value-abc"


def make_settings():
    return Settings(mode="demo", rest_base="https://demo-api.binance.com", stream_base="wss://x",
                    api_key=KEY, api_secret=SECRET, state_dir=Path("."), starting_capital=1000.0,
                    auto_git_push=False)


class FakeResponse:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """items: dict/list/bytes(성공 본문) | ('http', status, body, headers) | Exception"""
    def __init__(self, *items):
        self.items = list(items)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            _, status, body, headers = item
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            raise urllib.error.HTTPError(request.full_url, status, "err", headers or {}, io.BytesIO(raw))
        return FakeResponse(item)

    def query(self, i=-1):
        return {k: v[0] for k, v in parse_qs(urlsplit(self.requests[i].full_url).query).items()}


def http(status, body=None, headers=None):
    return ("http", status, body if body is not None else {}, headers)


def make(*items, calls=None, clock=lambda: 1_700_000_000.0):
    opener = FakeOpener(*items)
    on_call = (lambda name, res: calls.append((name, res))) if calls is not None else None
    return BinanceExchange(make_settings(), on_call=on_call, opener=opener, clock=clock), opener


ORDER_FILLS = {
    "symbol": "ETHUSDT", "orderId": 99, "clientOrderId": "cid1", "side": "BUY", "status": "FILLED",
    "executedQty": "0.50000000", "cummulativeQuoteQty": "1500.00000000",
    "fills": [
        {"price": "2990", "qty": "0.2", "commission": "0.0001", "commissionAsset": "ETH"},
        {"price": "3006.66666667", "qty": "0.3", "commission": "0.0002", "commissionAsset": "ETH"},
    ],
}


class BinanceRestTest(unittest.TestCase):
    def test_signed_request_has_key_header_timestamp_and_signature(self):
        ex, op = make({"balances": [{"asset": "USDT", "free": "10.5", "locked": "1"}]})
        res = ex.balances()
        self.assertTrue(res.ok)
        self.assertEqual(res.value, {"USDT": (10.5, 1.0)})
        req = op.requests[0]
        self.assertTrue(req.full_url.startswith("https://demo-api.binance.com/api/v3/account?"))
        self.assertEqual(req.get_header("X-mbx-apikey"), KEY)
        q = op.query()
        self.assertEqual(q["recvWindow"], "5000")
        self.assertEqual(q["timestamp"], "1700000000000")
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", q["signature"]))
        self.assertTrue(req.full_url.endswith("signature=" + q["signature"]))

    def test_signature_is_hmac_of_query(self):
        import hashlib
        import hmac
        ex, op = make({"balances": []})
        ex.balances()
        url = op.requests[0].full_url
        query, sig = url.split("?", 1)[1].rsplit("&signature=", 1)
        expected = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(sig, expected)

    def test_symbol_rules_parse_filters(self):
        def sym(name, base, quote, status, notional_type):
            return {"symbol": name, "baseAsset": base, "quoteAsset": quote, "status": status, "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.00001000"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                {"filterType": notional_type, "minNotional": "5.00000000"}]}
        info = {"symbols": [sym("ETHUSDT", "ETH", "USDT", "TRADING", "NOTIONAL"),
                            sym("BTCUSDT", "BTC", "USDT", "TRADING", "MIN_NOTIONAL"),
                            sym("OLDUSDT", "OLD", "USDT", "BREAK", "NOTIONAL"),
                            sym("ETHBTC", "ETH", "BTC", "TRADING", "NOTIONAL")]}
        ex, op = make(info)
        res = ex.symbol_rules()
        self.assertTrue(res.ok)
        rules = res.value
        self.assertEqual(set(rules), {"ETHUSDT", "BTCUSDT", "OLDUSDT"})
        eth = rules["ETHUSDT"]
        self.assertEqual((eth.base, eth.quote, eth.trading), ("ETH", "USDT", True))
        self.assertEqual((eth.step_size, eth.tick_size, eth.min_notional), (0.00001, 0.01, 5.0))
        self.assertEqual(rules["BTCUSDT"].min_notional, 5.0)
        self.assertFalse(rules["OLDUSDT"].trading)
        self.assertNotIn("signature", op.query())

    def test_daily_candles_oldest_first_with_utc_date(self):
        rows = [[1731801600000, "1", "2", "0.5", "1.5", "10", 1731887999999, "15.5", 1, "0", "0", "0"],
                [1731888000000, "1.5", "3", "1", "2", "20", 1731974399999, "40", 1, "0", "0", "0"]]
        ex, op = make(rows)
        res = ex.daily_candles("ETHUSDT", 2)
        self.assertTrue(res.ok)
        self.assertEqual(op.query()["interval"], "1d")
        self.assertEqual(op.query()["limit"], "2")
        self.assertEqual(res.value[0], {"date": "2024-11-17", "open": 1.0, "high": 2.0, "low": 0.5,
                                        "close": 1.5, "vol": 10.0, "quote_vol": 15.5})
        self.assertEqual(res.value[1]["date"], "2024-11-18")

    def test_market_buy_sends_full_response_type_and_parses_fills(self):
        ex, op = make(ORDER_FILLS)
        res = ex.market_buy("ETHUSDT", 0.5, "cid1")
        self.assertTrue(res.ok)
        self.assertEqual(op.requests[0].get_method(), "POST")
        q = op.query()
        self.assertEqual((q["type"], q["side"], q["newOrderRespType"], q["newClientOrderId"]),
                         ("MARKET", "BUY", "FULL", "cid1"))
        self.assertEqual(q["quantity"], "0.5")
        st = res.value
        self.assertIsInstance(st, OrderState)
        self.assertEqual((st.client_id, st.side, st.status), ("cid1", "BUY", "FILLED"))
        self.assertAlmostEqual(st.executed_qty, 0.5)
        self.assertAlmostEqual(st.quote_qty, 1500.0)
        self.assertEqual(set(st.fees), {"ETH"})
        self.assertAlmostEqual(st.fees["ETH"], 0.0003)

    def test_quantities_never_use_exponent_notation(self):
        ex, op = make(ORDER_FILLS)
        ex.market_sell("ETHUSDT", 0.00001, "c")
        self.assertEqual(op.query()["quantity"], "0.00001")
        self.assertEqual(op.query()["side"], "SELL")

    def test_place_stop_sends_stop_loss_limit(self):
        resp = dict(ORDER_FILLS, side="SELL", status="NEW", executedQty="0", cummulativeQuoteQty="0", fills=[])
        ex, op = make(resp)
        res = ex.place_stop("ETHUSDT", 0.5, 2900.0, 2871.0, "cid2")
        self.assertTrue(res.ok)
        q = op.query()
        self.assertEqual((q["type"], q["timeInForce"], q["side"]), ("STOP_LOSS_LIMIT", "GTC", "SELL"))
        self.assertEqual((q["stopPrice"], q["price"], q["newClientOrderId"]), ("2900", "2871", "cid2"))
        self.assertEqual(res.value.status, "NEW")

    def test_cancel_uses_orig_client_order_id(self):
        resp = {"symbol": "ETHUSDT", "origClientOrderId": "cid2", "clientOrderId": "cancel-xyz",
                "side": "SELL", "status": "CANCELED", "executedQty": "0", "cummulativeQuoteQty": "0"}
        ex, op = make(resp)
        res = ex.cancel_order("ETHUSDT", "cid2")
        self.assertEqual(op.requests[0].get_method(), "DELETE")
        self.assertEqual(op.query()["origClientOrderId"], "cid2")
        self.assertEqual(res.value.client_id, "cid2")
        self.assertEqual(res.value.status, "CANCELED")

    def test_get_order_fills_fees_from_my_trades(self):
        order = dict(ORDER_FILLS)
        order.pop("fills")
        trades = [{"commission": "0.0001", "commissionAsset": "ETH"},
                  {"commission": "0.5", "commissionAsset": "BNB"},
                  {"commission": "0.0002", "commissionAsset": "ETH"}]
        ex, op = make(order, trades)
        res = ex.get_order("ETHUSDT", "cid1")
        self.assertTrue(res.ok)
        self.assertEqual(op.query(0)["origClientOrderId"], "cid1")
        self.assertIn("/api/v3/myTrades", op.requests[1].full_url)
        self.assertEqual(op.query(1)["orderId"], "99")
        self.assertAlmostEqual(res.value.fees["ETH"], 0.0003)
        self.assertAlmostEqual(res.value.fees["BNB"], 0.5)

    def test_get_order_without_execution_skips_my_trades(self):
        order = dict(ORDER_FILLS, status="NEW", executedQty="0", cummulativeQuoteQty="0")
        order.pop("fills")
        ex, op = make(order)
        res = ex.get_order("ETHUSDT", "cid1")
        self.assertTrue(res.ok)
        self.assertEqual(len(op.requests), 1)
        self.assertEqual(res.value.fees, {})

    def test_get_order_fails_when_my_trades_fails(self):
        order = dict(ORDER_FILLS)
        order.pop("fills")
        ex, _ = make(order, http(503))
        res = ex.get_order("ETHUSDT", "cid1")
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, "transient")

    def test_open_orders(self):
        ex, op = make([dict(ORDER_FILLS, status="NEW", executedQty="0", cummulativeQuoteQty="0", fills=[])])
        res = ex.open_orders("ETHUSDT")
        self.assertTrue(res.ok)
        self.assertEqual(op.query()["symbol"], "ETHUSDT")
        self.assertEqual([o.client_id for o in res.value], ["cid1"])
        ex, op = make([])
        self.assertTrue(ex.open_orders().ok)
        self.assertNotIn("symbol", op.query())

    def test_unknown_order_is_ok_but_not_exists(self):
        ex, _ = make(http(400, {"code": -2013, "msg": "Order does not exist."}))
        res = ex.get_order("ETHUSDT", "nope")
        self.assertTrue(res.ok)
        self.assertIs(res.value.exists, False)
        self.assertEqual(res.value.client_id, "nope")

    def test_error_classification(self):
        cases = [
            (http(503), "transient", None),
            (http(429, {}, {"Retry-After": "7"}), "transient", 7),
            (http(400, {"code": -2010, "msg": "insufficient"}), "permanent", None),
            (http(400, {"code": -1013, "msg": "filter"}), "permanent", None),
            (http(401, {"code": -2015, "msg": "bad key"}), "fatal", None),
            (http(403, {}), "fatal", None),
            (http(400, {"code": -2014, "msg": "bad key format"}), "fatal", None),
            (http(429, {"code": -1003, "msg": "too many"}), "transient", None),
            (http(503, {"code": -1007, "msg": "timeout"}), "transient", None),
            (urllib.error.URLError("dns"), "transient", None),
            (TimeoutError("slow"), "transient", None),
        ]
        for item, kind, retry in cases:
            ex, _ = make(item)
            res = ex.balances()
            self.assertFalse(res.ok, item)
            self.assertEqual(res.kind, kind, item)
            if retry is not None:
                self.assertEqual(res.retry_after, retry)
        ex, _ = make(http(418))
        res = ex.balances()
        self.assertEqual(res.kind, "transient")
        self.assertGreater(res.retry_after, 0)

    def test_bad_json_is_failure(self):
        ex, _ = make(b"<html>")
        res = ex.server_time()
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, "transient")

    def test_unexpected_shape_is_failure_not_empty(self):
        ex, _ = make({"nothing": 1})
        res = ex.balances()
        self.assertFalse(res.ok)

    def test_clock_skew_resyncs_and_retries_once(self):
        ex, op = make(http(400, {"code": -1021, "msg": "ahead"}), {"serverTime": 1_700_000_100_000},
                      {"balances": []})
        res = ex.balances()
        self.assertTrue(res.ok)
        self.assertEqual(len(op.requests), 3)
        self.assertIn("/api/v3/time", op.requests[1].full_url)
        self.assertEqual(op.query(0)["timestamp"], "1700000000000")
        self.assertEqual(op.query(2)["timestamp"], "1700000100000")

    def test_clock_skew_gives_up_after_one_retry(self):
        ex, op = make(http(400, {"code": -1021}), {"serverTime": 1_700_000_100_000}, http(400, {"code": -1021}))
        res = ex.balances()
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, "transient")
        self.assertEqual(len(op.requests), 3)

    def test_on_call_reported_for_every_http_attempt(self):
        calls = []
        ex, _ = make(http(400, {"code": -1021}), {"serverTime": 1_700_000_100_000}, {"balances": []}, calls=calls)
        ex.balances()
        self.assertEqual([n for n, _ in calls], ["account", "time", "account"])
        self.assertEqual([r.ok for _, r in calls], [False, True, True])
        calls = []
        ex, _ = make(urllib.error.URLError("x"), calls=calls)
        ex.balances()
        self.assertEqual(len(calls), 1)

    def test_secret_never_in_error_text(self):
        for item in (urllib.error.URLError(f"boom {SECRET} {KEY}"),
                     http(400, {"code": -1100, "msg": f"bad {SECRET}"}),
                     http(500, f"oops {SECRET}".encode())):
            calls = []
            ex, op = make(item, calls=calls)
            res = ex.balances()
            sig = op.query()["signature"]
            self.assertFalse(res.ok)
            for text in (res.error, repr(calls)):
                for secret in (SECRET, KEY, sig):
                    self.assertNotIn(secret, text)

    def test_order_post_unparseable_success_is_outcome_unknown_transient(self):
        calls = []
        ex, _ = make({"unexpected": 1}, calls=calls)
        res = ex.market_buy("ETHUSDT", 0.5, "cid1")
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, "transient")
        self.assertIn("get_order", res.error)
        ex, _ = make({"unexpected": 1})
        res = ex.place_stop("ETHUSDT", 0.5, 2900.0, 2871.0, "cid2")
        self.assertEqual((res.ok, res.kind), (False, "transient"))
        ex, _ = make({"unexpected": 1})
        res = ex.cancel_order("ETHUSDT", "cid2")
        self.assertEqual((res.ok, res.kind), (False, "transient"))
        ex, _ = make(b"<html>")
        res = ex.market_sell("ETHUSDT", 0.5, "cid3")
        self.assertEqual((res.ok, res.kind), (False, "transient"))
        self.assertIn("get_order", res.error)

    def test_invalid_header_value_is_fatal_and_reported_once(self):
        calls = []
        ex, _ = make(ValueError(f"Invalid header value b'{KEY}\n'"), calls=calls)
        res = ex.balances()
        self.assertEqual((res.ok, res.kind), (False, "fatal"))
        self.assertNotIn(KEY, res.error)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "account")
        self.assertNotIn(KEY, repr(calls))

    def test_unexpected_exceptions_become_transient_results(self):
        for exc in (RecursionError("deep " + SECRET), RuntimeError(SECRET), UnicodeEncodeError("latin-1", "x", 0, 1, SECRET)):
            calls = []
            ex, _ = make(exc, calls=calls)
            res = ex.server_time()
            self.assertFalse(res.ok)
            self.assertNotIn(SECRET, res.error)
            self.assertIn(type(exc).__name__, res.error)
            self.assertEqual(len(calls), 1)
        ex, _ = make(RuntimeError("x"))
        self.assertEqual(ex.server_time().kind, "transient")

    def test_urls_always_use_rest_base(self):
        ex, op = make({"serverTime": 5})
        ex.server_time()
        self.assertTrue(op.requests[0].full_url.startswith("https://demo-api.binance.com/"))


if __name__ == "__main__":
    unittest.main()
