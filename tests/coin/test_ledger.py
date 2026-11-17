import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coin_sniper.ledger import (  # noqa: E402
    AlreadyRunningError,
    InstanceLock,
    StateCorruptError,
    StateStore,
    add_intent,
    new_client_id,
    record_event,
)

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)


class StateStoreTests(unittest.TestCase):
    def test_save_is_atomic_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "nested" / "state")
            data = {"BTCUSDT": {"qty": 0.5, "note": "한글"}}
            store.save("positions", data)
            self.assertEqual(store.load("positions", {}), data)
            raw = store.path("positions").read_text(encoding="utf-8")
            self.assertIn("한글", raw)
            self.assertEqual(json.loads(raw), data)
            self.assertEqual([p.name for p in store.path("positions").parent.iterdir()], ["positions.json"])
            store.save("positions", {"ETHUSDT": {"qty": 1}})
            self.assertEqual(store.load("positions", {}), {"ETHUSDT": {"qty": 1}})

    def test_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            default = {"a": [1]}
            result = store.load("trades", default)
            self.assertEqual(result, default)
            result["a"].append(2)
            self.assertEqual(default, {"a": [1]})

    def test_corrupt_file_raises_instead_of_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            path = store.path("positions")
            path.write_text('{"BTCUSDT": {"qty": 0.0', encoding="utf-8")
            with self.assertRaises(StateCorruptError) as ctx:
                store.load("positions", {})
            self.assertIn("positions.json", str(ctx.exception))
            self.assertNotIn("BTCUSDT", str(ctx.exception))

    def test_empty_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            store.path("intents").write_text("", encoding="utf-8")
            with self.assertRaises(StateCorruptError):
                store.load("intents", {})

    def test_unknown_name_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            with self.assertRaises(ValueError):
                store.save("../evil", {})
            with self.assertRaises(ValueError):
                store.load("nope", {})


class IntentTests(unittest.TestCase):
    def test_client_id_is_exchange_safe_and_unique(self):
        ids = {new_client_id("demo", "entry") for _ in range(200)}
        self.assertEqual(len(ids), 200)
        for cid in ids:
            self.assertRegex(cid, r"^cs-d-en-[0-9a-f]{12}$")
            self.assertLessEqual(len(cid), 36)
        self.assertTrue(new_client_id("live", "exit").startswith("cs-l-ex-"))
        self.assertTrue(new_client_id("demo", "protective").startswith("cs-d-pr-"))
        with self.assertRaises(ValueError):
            new_client_id("paper", "entry")
        with self.assertRaises(ValueError):
            new_client_id("demo", "other")

    def test_add_intent_records_and_rejects_duplicate(self):
        intents = {}
        cid = new_client_id("demo", "entry")
        out = add_intent(intents, cid, "BTCUSDT", "BUY", 0.01, "entry", "signal", NOW)
        self.assertIs(out, intents)
        self.assertEqual(
            intents[cid],
            {
                "client_id": cid, "symbol": "BTCUSDT", "side": "BUY", "qty": 0.01,
                "kind": "entry", "reason": "signal", "created_at": NOW.isoformat(),
            },
        )
        with self.assertRaises(ValueError):
            add_intent(intents, cid, "BTCUSDT", "BUY", 0.01, "entry", "signal", NOW)


class EventTests(unittest.TestCase):
    def test_events_keep_seven_days_and_merge_repeated_skips(self):
        old = {"type": "BUY", "symbol": "A", "reason": "x",
               "timestamp": (NOW - timedelta(days=8)).isoformat(timespec="seconds")}
        fresh = {"type": "BUY", "symbol": "B", "reason": "y",
                 "timestamp": (NOW - timedelta(days=6)).isoformat(timespec="seconds")}
        bad = {"type": "BUY", "symbol": "C", "reason": "z", "timestamp": "garbage"}
        events = [old, fresh, bad]
        snapshot = json.loads(json.dumps(events))
        events2 = record_event(events, {"type": "SKIP", "symbol": "BTCUSDT", "reason": "rsi"}, NOW)
        self.assertEqual(events, snapshot)  # input not mutated
        self.assertEqual([e["symbol"] for e in events2], ["B", "BTCUSDT"])
        self.assertEqual(events2[-1]["count"], 1)
        self.assertEqual(events2[-1]["timestamp"], NOW.isoformat(timespec="seconds"))
        later = NOW + timedelta(minutes=5)
        events3 = record_event(events2, {"type": "SKIP", "symbol": "BTCUSDT", "reason": "rsi"}, later)
        self.assertEqual(len(events3), 2)
        self.assertEqual(events3[-1]["count"], 2)
        self.assertEqual(events3[-1]["timestamp"], later.isoformat(timespec="seconds"))
        events4 = record_event(events3, {"type": "SKIP", "symbol": "BTCUSDT", "reason": "other"}, later)
        self.assertEqual(len(events4), 3)

    def test_repeated_risk_merges_but_buy_does_not(self):
        ev = []
        for _ in range(3):
            ev = record_event(ev, {"type": "RISK", "symbol": "BTCUSDT", "reason": "kill"}, NOW)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["count"], 3)
        for _ in range(2):
            ev = record_event(ev, {"type": "BUY", "symbol": "BTCUSDT", "reason": "kill"}, NOW)
        self.assertEqual(len(ev), 3)

    def test_events_capped_at_500(self):
        ev = []
        for i in range(520):
            ev = record_event(ev, {"type": "BUY", "symbol": f"S{i}", "reason": "r"}, NOW)
        self.assertEqual(len(ev), 500)
        self.assertEqual(ev[-1]["symbol"], "S519")


class InstanceLockTests(unittest.TestCase):
    def test_second_lock_on_same_path_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.lock"
            first = InstanceLock(path)
            second = InstanceLock(path)
            first.acquire()
            try:
                with self.assertRaises(AlreadyRunningError):
                    second.acquire()
            finally:
                first.release()
                second.release()

    def test_lock_released_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.lock"
            lock = InstanceLock(path)
            lock.acquire()
            lock.release()
            lock.release()  # idempotent
            again = InstanceLock(path)
            again.acquire()
            again.release()

    def test_stale_lock_file_from_dead_process_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.lock"
            path.write_text("999999999", encoding="utf-8")
            with InstanceLock(path):
                pass

    def test_context_manager_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.lock"
            with InstanceLock(path):
                with self.assertRaises(AlreadyRunningError):
                    InstanceLock(path).acquire()
            with InstanceLock(path):
                pass


if __name__ == "__main__":
    unittest.main()
