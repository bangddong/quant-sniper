import json
import unittest
from datetime import datetime, timedelta, timezone

from coin_sniper import halts
from coin_sniper.halts import HaltBoard

T0 = datetime(2026, 11, 17, 3, 0, 0, tzinfo=timezone.utc)


class HaltBoardTest(unittest.TestCase):
    def test_codes_are_lowercase_names(self):
        for name in ("ACCOUNT_FETCH", "FEED_STALE", "MAINTENANCE", "RATE_LIMIT", "UNIVERSE", "DAILY_LOSS",
                     "DRAWDOWN", "RECON_MISMATCH", "ORDER_UNCONFIRMED", "PROTECTIVE_CANCEL", "FATAL",
                     "LOOP_ERROR"):
            self.assertEqual(getattr(halts, name), name.lower())

    def test_halt_board_roundtrip_and_order(self):
        board = HaltBoard()
        board.set(halts.DRAWDOWN, "dd 9%", T0)
        board.set(halts.FEED_STALE, "stale", T0 + timedelta(seconds=5))
        self.assertTrue(board.is_set(halts.DRAWDOWN))
        self.assertFalse(board.is_set(halts.FATAL))
        self.assertEqual([h["code"] for h in board.active()], ["drawdown", "feed_stale"])
        self.assertEqual(board.active()[0]["since"], T0.isoformat())
        self.assertEqual(board.since(halts.FEED_STALE), T0 + timedelta(seconds=5))
        self.assertIsNone(board.since(halts.FATAL))
        data = json.loads(json.dumps(board.to_dict()))
        restored = HaltBoard.from_dict(data)
        self.assertEqual(restored.active(), board.active())
        self.assertEqual(restored.since(halts.DRAWDOWN), T0)
        self.assertEqual(restored.to_dict(), board.to_dict())
        board.clear(halts.DRAWDOWN)
        board.clear(halts.FATAL)  # no-op
        self.assertEqual([h["code"] for h in board.active()], ["feed_stale"])

    def test_set_twice_keeps_original_since(self):
        board = HaltBoard()
        board.set(halts.RATE_LIMIT, "first", T0)
        board.set(halts.RATE_LIMIT, "second", T0 + timedelta(minutes=1))
        self.assertEqual(board.since(halts.RATE_LIMIT), T0)
        self.assertEqual(board.active(), [{"code": "rate_limit", "detail": "second", "since": T0.isoformat()}])


if __name__ == "__main__":
    unittest.main()
