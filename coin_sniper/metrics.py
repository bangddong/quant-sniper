"""오류율·가동률 카운터와 자산 이력. 카운터는 UTC 날짜별 버킷에 쌓는다. 시간은 항상 now 인자."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from coin_sniper.config import METRICS_RETENTION_DAYS, POLL_INTERVAL_SECONDS
from coin_sniper.models import ApiResult

_DAY_SECONDS = 86400
_ORDER_FIELDS = {
    "sent": "orders_sent",
    "rejected": "orders_rejected",
    "unconfirmed": "orders_unconfirmed",
    "partial": "orders_partial",
    "filled": "orders_filled",
}
_SUM_FIELDS = (
    "api_total", "api_failed", "api_transient", "api_permanent",
    "orders_sent", "orders_rejected", "orders_unconfirmed", "orders_partial",
    "feed_reconnects", "feed_down_seconds", "offline_seconds",
    "loops_ok", "loops_failed", "restarts",
)


def _utc(now: datetime) -> datetime:
    return now.astimezone(timezone.utc)


def _new_bucket() -> dict:
    bucket: dict = {name: 0 for name in _SUM_FIELDS}
    bucket["orders_filled"] = 0
    bucket["halt_seconds"] = {}
    bucket["endpoints"] = {}
    return bucket


class Metrics:
    def __init__(self, data: dict | None = None) -> None:
        self._days: dict[str, dict] = {}
        for key, stored in ((data or {}).get("days") or {}).items():
            bucket = _new_bucket()
            bucket.update({k: v for k, v in stored.items() if k not in ("halt_seconds", "endpoints")})
            bucket["halt_seconds"] = dict(stored.get("halt_seconds", {}))
            bucket["endpoints"] = {e: dict(v) for e, v in stored.get("endpoints", {}).items()}
            self._days[key] = bucket

    def _bucket(self, now: datetime) -> dict:
        today = _utc(now).date()
        cutoff = (today - timedelta(days=METRICS_RETENTION_DAYS)).isoformat()
        for key in [k for k in self._days if k < cutoff]:
            del self._days[key]
        return self._days.setdefault(today.isoformat(), _new_bucket())

    def record_api(self, endpoint: str, result: ApiResult, now: datetime) -> None:
        b = self._bucket(now)
        ep = b["endpoints"].setdefault(endpoint, {"total": 0, "failed": 0})
        b["api_total"] += 1
        ep["total"] += 1
        if result.ok:
            return
        b["api_failed"] += 1
        ep["failed"] += 1
        b["api_transient" if result.kind == "transient" else "api_permanent"] += 1

    def record_order(self, event: str, now: datetime) -> None:
        self._bucket(now)[_ORDER_FIELDS[event]] += 1

    def record_loop(self, ok: bool, now: datetime) -> None:
        self._bucket(now)["loops_ok" if ok else "loops_failed"] += 1

    def record_feed_reconnect(self, now: datetime) -> None:
        self._bucket(now)["feed_reconnects"] += 1

    def add_seconds(self, bucket: str, seconds: float, now: datetime) -> None:
        b = self._bucket(now)
        if bucket == "feed_down":
            b["feed_down_seconds"] += seconds
        elif bucket == "offline":
            b["offline_seconds"] += seconds
        elif bucket.startswith("halt:"):
            code = bucket[len("halt:"):]
            b["halt_seconds"][code] = b["halt_seconds"].get(code, 0) + seconds
        else:
            raise ValueError(f"알 수 없는 시간 버킷: {bucket!r}")

    def record_restart(self, now: datetime) -> None:
        self._bucket(now)["restarts"] += 1

    def summary(self, now: datetime, days: int | None) -> dict:
        now_utc = _utc(now)
        today = now_utc.date()
        if days is None:
            keys = sorted(self._days)
            start = date.fromisoformat(keys[0]) if keys else today
        else:
            start = today - timedelta(days=days - 1)
        window = [b for k, b in self._days.items() if start.isoformat() <= k <= today.isoformat()]

        total = {name: sum(b[name] for b in window) for name in _SUM_FIELDS}
        halt_seconds: dict[str, float] = {}
        for b in window:
            for code, secs in b["halt_seconds"].items():
                halt_seconds[code] = halt_seconds.get(code, 0) + secs

        elapsed_today = (now_utc - now_utc.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()
        loop_days = sorted(k for k, b in self._days.items() if b["loops_ok"] + b["loops_failed"] > 0)
        if loop_days:                                      # 기록보다 긴 기간: 루프가 처음 기록된 날부터
            start = max(start, date.fromisoformat(loop_days[0]))
        n_days = max((today - start).days + 1, 0)
        period = max((n_days - 1) * _DAY_SECONDS + elapsed_today, 0) if n_days else 0
        uptime = min(total["loops_ok"] * POLL_INTERVAL_SECONDS / period * 100, 100.0) if period > 0 else 0.0
        fail_rate = total["api_failed"] / total["api_total"] * 100 if total["api_total"] else 0.0

        return {
            "api_total": total["api_total"],
            "api_failed": total["api_failed"],
            "api_fail_rate": round(fail_rate, 2),
            "api_transient": total["api_transient"],
            "api_permanent": total["api_permanent"],
            "orders_sent": total["orders_sent"],
            "orders_rejected": total["orders_rejected"],
            "orders_unconfirmed": total["orders_unconfirmed"],
            "orders_partial": total["orders_partial"],
            "feed_reconnects": total["feed_reconnects"],
            "feed_down_seconds": total["feed_down_seconds"],
            "halt_seconds": halt_seconds,
            "offline_seconds": total["offline_seconds"],
            "loops_ok": total["loops_ok"],
            "loops_failed": total["loops_failed"],
            "uptime_pct": round(uptime, 2),
            "restarts": total["restarts"],
        }

    def to_dict(self) -> dict:
        return {"days": {
            k: {**{n: v for n, v in b.items() if n not in ("halt_seconds", "endpoints")},
                "halt_seconds": dict(b["halt_seconds"]),
                "endpoints": {e: dict(v) for e, v in b["endpoints"].items()}}
            for k, b in sorted(self._days.items())
        }}


def append_equity(history: list[dict], day: str, equity: float, btc_close: float,
                  segment: str | None = None) -> list[dict]:
    """segment: 그 줄이 속한 수익률 구간(risk_state.segment_start). 주면 줄에 남긴다."""
    rows = [r for r in history if r["day"] != day]
    row = {"day": day, "equity": equity, "btc": btc_close}
    if segment is not None:
        row["segment"] = segment
    rows.append(row)
    rows.sort(key=lambda r: r["day"])
    return rows


def _pct(base: float, current: float) -> float | None:
    if not base:
        return None
    return round((current / base - 1) * 100, 2)


def period_returns(history: list[dict], today_equity: float, today_btc: float) -> dict:
    lookbacks = {"1d": 1, "7d": 7, "30d": 30}
    result: dict = {}
    for key, back in lookbacks.items():
        result[key] = _compare(history[-back] if len(history) >= back else None, today_equity, today_btc)
    result["all"] = _compare(history[0] if history else None, today_equity, today_btc)
    return result


def _compare(row: dict | None, equity: float, btc: float) -> dict:
    if row is None:
        return {"bot": None, "btc": None}
    return {"bot": _pct(row["equity"], equity), "btc": _pct(row["btc"], btc)}
