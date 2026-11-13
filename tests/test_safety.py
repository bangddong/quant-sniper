import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from bot import save_json, trading_days_held
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
    def test_live_mode_requires_explicit_enable(self):
        with patch.object(config, "KIWOOM_USE_MOCK", False), \
             patch.object(config, "LIVE_TRADING_ENABLED", False):
            with self.assertRaises(RuntimeError):
                KiwoomRestAPI("key", "secret", "account")

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
        api = KiwoomRestAPI("key", "secret", "account", is_mock=True)
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
        api = KiwoomRestAPI("key", "secret", "account", is_mock=True)
        api.get_token = lambda: "token"
        result = api.get_order_execution("123", "005930")
        self.assertEqual(result["filled_qty"], 3)
        self.assertEqual(result["fill_price"], 10100)

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
