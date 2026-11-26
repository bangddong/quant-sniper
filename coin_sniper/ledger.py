"""로컬 상태 저장, 주문 의도, 이벤트 기록, 단일 실행 잠금.

손상된 상태 파일은 빈 장부로 대체하지 않고 StateCorruptError로 중단한다.
"""
from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from coin_sniper.config import EVENT_RETENTION_DAYS

try:  # 플랫폼별 파일 잠금: 둘 다 import 실패를 허용한다.
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]
try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

STATE_NAMES = frozenset({
    "positions", "trades", "intents", "risk_state", "universe",
    "events", "metrics", "equity_history", "halts", "dust", "asset_exclusions",
})
_MODE_CODES = {"demo": "d", "live": "l"}
_KIND_CODES = {"entry": "en", "exit": "ex", "protective": "pr"}
_MAX_EVENTS = 500
_MERGEABLE_EVENT_TYPES = frozenset({"SKIP", "RISK"})


class StateCorruptError(RuntimeError):
    pass


class AlreadyRunningError(RuntimeError):
    pass


class StateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)

    def path(self, name: str) -> Path:
        if name not in STATE_NAMES:
            raise ValueError(f"알 수 없는 상태 이름: {name!r}")
        return self.state_dir / f"{name}.json"

    def load(self, name: str, default):
        path = self.path(name)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return copy.deepcopy(default)
        except OSError as exc:
            raise StateCorruptError(f"상태 파일을 읽을 수 없습니다: {path.name}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # 내용은 메시지에 싣지 않는다 (파일명만).
            raise StateCorruptError(f"상태 파일이 손상되었습니다: {path.name}") from None

    def save(self, name: str, data) -> None:
        path = self.path(name)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(path.name + ".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)


def new_client_id(mode: str, kind: str) -> str:
    if mode not in _MODE_CODES:
        raise ValueError(f"알 수 없는 mode: {mode!r}")
    if kind not in _KIND_CODES:
        raise ValueError(f"알 수 없는 kind: {kind!r}")
    return f"cs-{mode[0]}-{_KIND_CODES[kind]}-{uuid.uuid4().hex[:12]}"


def add_intent(intents: dict, client_id: str, symbol: str, side: str, qty: float,
               kind: str, reason: str, now: datetime) -> dict:
    if client_id in intents:
        raise ValueError(f"이미 존재하는 client_id: {client_id}")
    intents[client_id] = {
        "client_id": client_id,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "kind": kind,
        "reason": reason,
        "created_at": now.isoformat(),
    }
    return intents


def _event_key(item: dict) -> tuple:
    return (item.get("type"), item.get("symbol"), item.get("reason"))


def record_event(events: list[dict], event: dict, now: datetime) -> list[dict]:
    """최근 EVENT_RETENTION_DAYS일만 유지하고 반복 SKIP/RISK는 한 항목의 횟수로 합친다."""
    return record_event_merged(events, event, now)[0]


def record_event_merged(events: list[dict], event: dict, now: datetime) -> tuple[list[dict], bool]:
    """record_event와 같고, 이 이벤트가 기존 항목에 합쳐졌는지(True) 새 항목인지(False)도 돌려준다."""
    cutoff = now - timedelta(days=EVENT_RETENTION_DAYS)
    retained = []
    for item in events:
        try:
            timestamp = datetime.fromisoformat(item.get("timestamp", ""))
            if timestamp >= cutoff:
                retained.append(dict(item))
        except (TypeError, ValueError):
            continue

    event = dict(event)
    event.setdefault("timestamp", now.isoformat(timespec="seconds"))
    if event.get("type") in _MERGEABLE_EVENT_TYPES:
        key = _event_key(event)
        for item in reversed(retained):
            if _event_key(item) == key and item.get("type") == event["type"]:
                item["timestamp"] = event["timestamp"]
                item["count"] = int(item.get("count", 1)) + 1
                return retained[-_MAX_EVENTS:], True
    event.setdefault("count", 1)
    retained.append(event)
    return retained[-_MAX_EVENTS:], False


class InstanceLock:
    """OS 파일 잠금(Windows msvcrt / POSIX fcntl). 프로세스가 죽으면 OS가 잠금을 풀어 준다."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise AlreadyRunningError("이 InstanceLock은 이미 잠금을 보유 중입니다")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            self._lock_fd(fd)
        except OSError as exc:
            os.close(fd)
            raise AlreadyRunningError(f"이미 실행 중입니다: {self.path}") from exc
        except BaseException:
            os.close(fd)
            raise
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError:
            pass  # PID 기록은 참고용이다; 잠금 자체가 본체.
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            self._unlock_fd(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    @staticmethod
    def _lock_fd(fd: int) -> None:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:  # pragma: no cover
            raise RuntimeError("이 플랫폼에서는 파일 잠금을 지원하지 않습니다")

    @staticmethod
    def _unlock_fd(fd: int) -> None:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
