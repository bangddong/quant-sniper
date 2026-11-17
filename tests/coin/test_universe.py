import unittest

from coin_sniper import config
from coin_sniper.models import SymbolRules
from coin_sniper.universe import is_excluded, select_universe


def rules(base, quote="USDT", trading=True):
    return SymbolRules(symbol=base + quote, base=base, quote=quote, trading=trading,
                       step_size=0.001, tick_size=0.01, min_notional=5.0)


def candles(prev_quote_vol, today_quote_vol=1.0, n=70):
    out = [{"date": "d", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1,
            "quote_vol": 5e7} for _ in range(n)]
    out[-2]["quote_vol"] = prev_quote_vol
    out[-1]["quote_vol"] = today_quote_vol
    return out


class UniverseTests(unittest.TestCase):
    def test_ranks_by_previous_completed_day_not_forming_candle(self):
        r = {"AUSDT": rules("A"), "BUSDT": rules("B")}
        c = {"AUSDT": candles(9e7, 1e9), "BUSDT": candles(1e8, 1e6)}
        self.assertEqual(select_universe(r, c), ["BUSDT", "AUSDT"])

    def test_excludes_stable_leveraged_untradable_short_history_low_volume(self):
        r = {
            "USDCUSDT": rules("USDC"), "BTCUPUSDT": rules("BTCUP"),
            "HALTUSDT": rules("HALT", trading=False),
            "SHORTUSDT": rules("SHORT"), "LOWUSDT": rules("LOW"),
            "BTCBUSD": rules("BTC", quote="BUSD"),
        }
        c = {s: candles(5e8) for s in r}
        c["SHORTUSDT"] = candles(5e8, n=config.MIN_HISTORY_DAYS - 1)
        c["LOWUSDT"] = candles(config.MIN_PREV_DAY_QUOTE_VOLUME - 1)
        self.assertEqual(select_universe(r, c), [])
        # 캔들 60개, 전일 거래대금 1e7도 제외
        self.assertEqual(select_universe({"AUSDT": rules("A")}, {"AUSDT": candles(1e7, n=60)}), [])

    def test_is_excluded_suffix_rule(self):
        self.assertFalse(is_excluded(rules("UP")))
        self.assertTrue(is_excluded(rules("BTCUP")))
        self.assertTrue(is_excluded(rules("ETHDOWN")))
        self.assertFalse(is_excluded(rules("SOL")))

    def test_returns_at_most_top_n(self):
        n = config.UNIVERSE_TOP_N + 5
        r = {f"C{i}USDT": rules(f"C{i}") for i in range(n)}
        c = {s: candles(1e8 + i) for i, s in enumerate(r)}
        out = select_universe(r, c)
        self.assertEqual(len(out), config.UNIVERSE_TOP_N)
        self.assertEqual(out[0], f"C{n - 1}USDT")

    def test_proxy_symbol_can_be_in_universe(self):
        r = {"BTCUSDT": rules("BTC")}
        self.assertEqual(select_universe(r, {"BTCUSDT": candles(5e9)}), ["BTCUSDT"])

    def test_missing_or_tiny_candle_lists_dropped_and_ties_by_symbol(self):
        r = {"ZUSDT": rules("Z"), "AUSDT": rules("A"), "MUSDT": rules("M"), "NUSDT": rules("N")}
        c = {"ZUSDT": candles(1e8), "AUSDT": candles(1e8), "NUSDT": candles(1e8)[-1:]}
        self.assertEqual(select_universe(r, c), ["AUSDT", "ZUSDT"])


if __name__ == "__main__":
    unittest.main()
