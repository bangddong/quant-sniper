import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from bot import (
    build_operational_status, confirm_persistent_signals, git_commit_and_push, is_entry_window,
    is_market_open, market_session_progress, save_json, trading_days_held,
)
from market_status import get_live_market_status
from kiwoom_api import KiwoomRestAPI
from screener import get_dynamic_universe


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class SafetyTests(unittest.TestCase):
    def test_operational_status_preserves_risk_reason(self):
        status, detail = build_operational_status(
            True, ["계좌 조회 실패", "계좌 조회 실패", "체결 미확인"], 1
        )
        self.assertEqual(status, "halted")
        self.assertEqual(detail, "계좌 조회 실패; 체결 미확인")

    def test_signal_must_persist_three_loops(self):
        signal = {"name": "후보주", "strategy": "변동성 돌파 (추세)"}
        counts = {}
        for expected in (0, 0, 1):
            confirmed, counts = confirm_persistent_signals([signal], counts, 3)
            self.assertEqual(len(confirmed), expected)
        confirmed, counts = confirm_persistent_signals([], counts, 3)
        self.assertEqual(confirmed, [])
        self.assertEqual(counts, {})

    @patch("bot.subprocess.run")
    def test_trade_push_failure_is_reported(self, run):
        import subprocess
        run.side_effect = subprocess.CalledProcessError(1, ["git", "push"])
        self.assertFalse(git_commit_and_push("test"))

    def test_entry_window_avoids_open_and_late_session(self):
        import datetime
        self.assertFalse(is_entry_window(datetime.datetime(2026, 11, 13, 9, 14)))
        self.assertTrue(is_entry_window(datetime.datetime(2026, 11, 13, 9, 15)))
        self.assertTrue(is_entry_window(datetime.datetime(2026, 11, 13, 14, 30)))
        self.assertFalse(is_entry_window(datetime.datetime(2026, 11, 13, 14, 31)))
        self.assertEqual(market_session_progress(datetime.datetime(2026, 11, 13, 9, 0)), 0)
        self.assertEqual(market_session_progress(datetime.datetime(2026, 11, 13, 15, 30)), 1)

    def test_market_open_requires_krx_trading_day(self):
        import datetime
        self.assertTrue(is_market_open(datetime.datetime(2026, 11, 13, 10, 0), True))
        self.assertFalse(is_market_open(datetime.datetime(2026, 11, 13, 10, 0), False))
        self.assertFalse(is_market_open(datetime.datetime(2026, 11, 14, 10, 0), True))
        self.assertFalse(is_market_open(datetime.datetime(2026, 11, 13, 8, 59), True))

    @patch("market_status.urllib.request.urlopen")
    def test_live_market_status_requires_today_and_all_open_flags(self, urlopen):
        import datetime
        urlopen.return_value = FakeResponse({
            "localTradedAt": "2026-11-13T10:31:17+09:00",
            "marketStatus": "OPEN", "marketStatusDetailType": "open",
            "marketSessionType": "regularMarket", "tradableStatus": "tradable",
            "tradeStopType": {"name": "TRADING"},
        })
        now = datetime.datetime(2026, 11, 13, 10, 32)
        self.assertTrue(get_live_market_status(now))
        now = datetime.datetime(2026, 11, 16, 10, 32)
        self.assertFalse(get_live_market_status(now))

    def test_live_mode_requires_explicit_enable(self):
        with patch.object(config, "LIVE_TRADING_ENABLED", False):
            with self.assertRaises(RuntimeError):
                KiwoomRestAPI("key", "secret", "account")

    def test_live_mode_requires_account_number(self):
        with patch.object(config, "LIVE_TRADING_ENABLED", True):
            with self.assertRaises(RuntimeError):
                KiwoomRestAPI("key", "secret", "")

    def test_atomic_json_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            save_json(path, {"ok": True})
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), {"ok": True})
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_holding_days_increment_once_per_date(self):
        pos = {"days_held": 1, "last_held_date": "2026-11-12"}
        self.assertEqual(trading_days_held(pos, "2026-11-13"), 2)
        self.assertEqual(trading_days_held(pos, "2026-11-13"), 2)

    @patch("urllib.request.urlopen")
    def test_account_snapshot_parses_broker_positions(self, urlopen):
        urlopen.return_value = FakeResponse({
            "return_code": 0,
            "prsm_dpst_aset_amt": "000000001000000",
            "tot_evlt_pl": "-00000000001000",
            "acnt_evlt_remn_indv_tot": [{
                "stk_cd": "A005930", "stk_nm": "삼성전자",
                "rmnd_qty": "000000000000003", "trde_able_qty": "3",
                "pur_pric": "000000000070000", "cur_prc": "71000",
                "evltv_prft": "3000",
            }],
        })
        with patch.object(config, "LIVE_TRADING_ENABLED", True):
            api = KiwoomRestAPI("key", "secret", "account")
        api.get_token = lambda: "token"
        api.get_cash_balance = lambda: 500000
        snapshot = api.get_account_snapshot()
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["positions"]["005930"]["shares"], 3)
        self.assertEqual(snapshot["positions"]["005930"]["buy_price"], 70000)

    @patch("urllib.request.urlopen")
    def test_execution_uses_weighted_actual_fill_price(self, urlopen):
        urlopen.return_value = FakeResponse({
            "return_code": 0,
            "acnt_ord_cntr_prst_array": [
                {"ord_no": "0000123", "cntr_qty": "2", "cntr_uv": "10000"},
                {"ord_no": "0000123", "cntr_qty": "1", "cntr_uv": "10300"},
            ],
        })
        with patch.object(config, "LIVE_TRADING_ENABLED", True):
            api = KiwoomRestAPI("key", "secret", "account")
        api.get_token = lambda: "token"
        result = api.get_order_execution("123", "005930")
        self.assertEqual(result["filled_qty"], 3)
        self.assertEqual(result["fill_price"], 10100)

    def test_kiwoom_previous_day_universe_uses_previous_market_cap(self):
        with patch.object(config, "LIVE_TRADING_ENABLED", True):
            api = KiwoomRestAPI("key", "secret", "account")
        api.get_token = lambda: "token"
        api._post_domestic = unittest.mock.Mock(side_effect=[
            {"return_code": 0, "pred_trde_qty_upper": [
                {"stk_cd": "000001", "stk_nm": "후보주"},
                {"stk_cd": "069500", "stk_nm": "KODEX200"},
            ]},
            {"return_code": 0, "lastPrice": "20000", "listCount": "100000000",
             "marketName": "KOSPI", "auditInfo": "", "orderWarning": "0"},
            {"return_code": 0, "lastPrice": "30000", "listCount": "100000000",
             "marketName": "ETF", "auditInfo": "", "orderWarning": "0"},
        ])
        universe = api.get_previous_day_universe(
            20, 1_000_000_000_000, 5_000_000_000_000
        )
        self.assertEqual(universe, {"후보주": "000001"})

    @patch("urllib.request.urlopen")
    def test_screener_excludes_etfs(self, urlopen):
        urlopen.return_value = FakeResponse({"stocks": [
            {"stockName": "개별주", "itemCode": "000001", "stockEndType": "stock",
             "closePriceRaw": "10000", "accumulatedTradingValueRaw": "100",
             "marketValueRaw": "2000000000000"},
            {"stockName": "KODEX 레버리지", "itemCode": "122630", "stockEndType": "etf",
             "closePriceRaw": "10000", "accumulatedTradingValueRaw": "999",
             "marketValueRaw": "2000000000000"},
        ]})
        universe = get_dynamic_universe(20)
        self.assertEqual(universe, {"개별주": "000001"})


if __name__ == "__main__":
    unittest.main()
