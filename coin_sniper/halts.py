"""신규 진입 중단 사유 보드. 시간은 항상 now 인자로 받는다."""
from __future__ import annotations

from datetime import datetime

ACCOUNT_FETCH = "account_fetch"
FEED_STALE = "feed_stale"
MAINTENANCE = "maintenance"
RATE_LIMIT = "rate_limit"
UNIVERSE = "universe"
DAILY_LOSS = "daily_loss"
DRAWDOWN = "drawdown"
RECON_MISMATCH = "recon_mismatch"
ORDER_UNCONFIRMED = "order_unconfirmed"
PROTECTIVE_CANCEL = "protective_cancel"
FATAL = "fatal"


class HaltBoard:
    def __init__(self) -> None:
        self._items: dict[str, dict] = {}     # 삽입 순서 = 설정 순서

    def set(self, code: str, detail: str, now: datetime) -> None:
        item = self._items.get(code)
        if item is None:
            self._items[code] = {"detail": detail, "since": now.isoformat()}
        else:
            item["detail"] = detail

    def clear(self, code: str) -> None:
        self._items.pop(code, None)

    def is_set(self, code: str) -> bool:
        return code in self._items

    def active(self) -> list[dict]:
        return [{"code": code, "detail": v["detail"], "since": v["since"]} for code, v in self._items.items()]

    def since(self, code: str) -> datetime | None:
        item = self._items.get(code)
        return datetime.fromisoformat(item["since"]) if item else None

    def to_dict(self) -> dict:
        return {"halts": self.active()}

    @classmethod
    def from_dict(cls, data: dict) -> "HaltBoard":
        board = cls()
        for h in data.get("halts", []):
            board._items[h["code"]] = {"detail": h["detail"], "since": h["since"]}
        return board
