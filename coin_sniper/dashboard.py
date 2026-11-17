"""수익률·오류율 대시보드 (스펙 8절): Markdown, 상태 JSON, dashboard 브랜치 게시, 게시 주기.

원본 bot.py의 render_dashboard / sync_dashboard_to_branch를 옮겼다. 입력은 Engine.snapshot(now) 하나다.
시각은 스냅숏의 now만 쓴다(현재 시각을 직접 읽지 않는다). 표시는 모두 KST(UTC+9).
인증정보(키·시크릿·서명)와 계좌 식별정보는 스냅숏에 없고, 여기서도 설정 객체를 받지 않는다.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from coin_sniper.config import (
    DASHBOARD_HEARTBEAT_SECONDS, DASHBOARD_MIN_INTERVAL_SECONDS, DASHBOARD_PUBLISH_TIMEOUT_SECONDS, QUOTE_ASSET,
    SIGNAL_CONFIRMATIONS,
)
from coin_sniper.halts import RECON_MISMATCH

KST = timezone(timedelta(hours=9), "KST")
COMMIT_MESSAGE = "dashboard: rolling 7-day snapshot"
RECONCILE_COMMAND = "python coin_bot.py reconcile"
STATUS_KO = {"running": "정상 운영", "halted": "신규 매수 중단", "error": "루프 오류"}
STRATEGY_KO = {"rsi_recovery": "RSI 회복", "vbo": "변동성 돌파"}
HALT_KO = {
    "account_fetch": "계좌 조회 실패", "feed_stale": "시세 지연", "maintenance": "거래소 점검·일시 오류",
    "rate_limit": "호출 한도", "universe": "감시 종목 없음", "daily_loss": "일일 손실 한도",
    "drawdown": "낙폭 한도", "recon_mismatch": "계좌·장부 불일치", "order_unconfirmed": "체결 미확인",
    "protective_cancel": "보호 주문 취소 확인", "fatal": "확정 오류", "loop_error": "루프 오류",
}
_MAX_EVENT_ROWS = 100


# ---- 서식 ---------------------------------------------------------------------------
def _md(value) -> str:
    """표 칸 하나. 모든 칸은 여기를 거친다(파이프·줄바꿈 이스케이프, None은 "-")."""
    text = "-" if value is None or value == "" else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _row(*cells) -> str:
    return "| " + " | ".join(_md(c) for c in cells) + " |"


def _table(header: list[str], align: str, rows: list[tuple], empty: str = "-") -> list[str]:
    lines = [_row(*header), "|" + "|".join("---:" if a == "r" else "---" for a in align) + "|"]
    lines += [_row(*r) for r in rows] or [_row(empty, *["-"] * (len(header) - 1))]
    return lines


def _utc(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _utc_day(iso: str | None):
    t = _utc(iso)
    return t.date() if t else None


def _kst(iso: str | None) -> str | None:
    t = _utc(iso)
    return t.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S") if t else None


def _num(value, fmt: str = ",.2f", suffix: str = "") -> str | None:
    return None if value is None else f"{value:{fmt}}{suffix}"


def _pct(value, signed: bool = True) -> str | None:
    return _num(value, "+.2f" if signed else ".2f", "%")


def _qty(value) -> str | None:
    return None if value is None else f"{value:,.8f}".rstrip("0").rstrip(".")


def _hours(seconds) -> str:
    return f"{(seconds or 0) / 3600:,.1f}시간"


# ---- 누적 성과 -------------------------------------------------------------------------
def _base(symbol: str) -> str:
    return symbol[:-len(QUOTE_ASSET)] if symbol.endswith(QUOTE_ASSET) else symbol


def _closed_stats(sells: list[dict]) -> tuple[int, float | None, float | None]:
    if not sells:
        return 0, None, None
    wins = sum(1 for t in sells if float(t.get("net_pnl") or 0) > 0)
    avg = sum(float(t.get("net_return_pct") or 0) for t in sells) / len(sells)
    return len(sells), round(wins / len(sells) * 100, 1), avg


def performance_stats(trades: list[dict], starting_capital: float | None = 1000.0) -> dict:
    """청산 성과. 청산 1건 = SELL 기록 1건(부분 매도도 각각), counted False(잔량 정리)는 뺀다.
    수수료·슬리피지는 모든 체결(BUY·SELL, counted 무관)에서 센다. 기초 자산 외 수수료 자산은 값을 매기지 않는다."""
    sells = sorted((t for t in trades if t.get("type") == "SELL" and t.get("counted")),
                   key=lambda t: str(t.get("time", "")))
    closed, win_rate, _ = _closed_stats(sells)
    pnls = [float(t.get("net_pnl") or 0) for t in sells]
    wins = [float(t.get("net_return_pct") or 0) for t, p in zip(sells, pnls) if p > 0]
    losses = [float(t.get("net_return_pct") or 0) for t, p in zip(sells, pnls) if p <= 0]
    gain, loss = sum(p for p in pnls if p > 0), -sum(p for p in pnls if p < 0)
    streak = max_streak = 0
    cum = peak = max_dd = 0.0
    for p in pnls:
        streak = streak + 1 if p <= 0 else 0
        max_streak = max(max_streak, streak)
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    fees, unpriced = 0.0, set()
    for t in trades:
        for asset, amount in (t.get("fees") or {}).items():
            if asset == QUOTE_ASSET:
                fees += float(amount)
            elif asset == _base(str(t.get("symbol", ""))) and t.get("price"):
                fees += float(amount) * float(t["price"])
            elif float(amount):
                unpriced.add(asset)
    slips = [float(t["slippage_pct"]) for t in trades if t.get("slippage_pct") is not None]
    by_strategy = {}
    for name in sorted({str(t.get("strategy") or "-") for t in sells}):
        n, rate, avg = _closed_stats([t for t in sells if str(t.get("strategy") or "-") == name])
        by_strategy[name] = {"closed": n, "win_rate": rate, "avg_return_pct": avg}
    return {
        "closed": closed, "win_rate": win_rate,
        "avg_win_pct": sum(wins) / len(wins) if wins else None,
        "avg_loss_pct": sum(losses) / len(losses) if losses else None,
        "profit_factor": gain / loss if loss else None,
        "max_loss_streak": max_streak, "total_net_pnl": sum(pnls), "total_fees_usdt": fees,
        "fees_unpriced_assets": sorted(unpriced),
        "avg_slippage_pct": sum(slips) / len(slips) if slips else None,
        "max_drawdown_pct": max_dd / starting_capital * 100 if starting_capital else None,
        "by_strategy": by_strategy,
    }


def _stats(snap: dict) -> dict:
    return performance_stats(snap.get("trades") or [], snap.get("starting_capital"))   # 모르면 최대 낙폭 "-"


# ---- Markdown ---------------------------------------------------------------------------
def _halt_label(h: dict) -> str:
    label = f"{HALT_KO.get(h.get('code'), h.get('code'))} ({h.get('code')})"
    if h.get("code") == RECON_MISMATCH and h.get("needs_manual"):
        label += f" — 수동 정리 필요: `{RECONCILE_COMMAND}`"
    return label


def _status_section(s: dict) -> list[str]:
    status = s.get("status")
    lines = ["## 1. 현재 상태", "", *_table(
        ["상태", "신규 매수", "보유 종목", "평가액(USDT)", "현금(USDT)"], "llrrr",
        [(f"{STATUS_KO.get(status, status)} ({status})", "가능" if s.get("entry_allowed") else "중단",
          len(s.get("positions") or []), _num(s.get("equity")), _num(s.get("cash")))])]
    halts = s.get("halts") or []
    if halts:
        lines += ["", *_table(["중단 사유", "상세", "시작"], "lll",
                              [(_halt_label(h), h.get("detail"), _kst(h.get("since"))) for h in halts])]
        if any(h.get("code") == RECON_MISMATCH and h.get("needs_manual") for h in halts):
            lines += ["", f"> **수동 정리 필요**: 거래소 잔고를 확인한 뒤 `{RECONCILE_COMMAND}` 를 실행하세요."]
    if s.get("entries_not_evaluated_reason"):
        lines += ["", f"> 이번 루프 진입 평가 안 함: {_md(s['entries_not_evaluated_reason'])}"]
    if not s.get("protective_stops_enabled", True):
        lines += ["", "> **거래소 손절 주문 비활성: 봇이 꺼져 있으면 손절이 실행되지 않습니다**"]
    if s.get("unmanaged_assets"):
        lines += ["", f"> 장부에 없는 거래소 잔고: {_md(', '.join(s['unmanaged_assets']))}"]
    lines += ["", "### 시스템 신선도", "", *_table(
        ["시세 연결", "마지막 시세", "마지막 계좌 조회", "마지막 정상 루프", "실행 커밋"], "lllll",
        [("정상" if s.get("feed_connected") else "끊김", _kst(s.get("last_quote_at")),
          _kst(s.get("last_account_sync")), _kst(s.get("last_loop_at")), s.get("commit"))])]
    if not s.get("feed_connected") and s.get("feed_error"):
        lines += ["", f"> 시세 연결 상태: {_md(s['feed_error'])}"]
    return lines


def _watch_section(s: dict) -> list[str]:
    universe = s.get("universe") or []
    allowed = bool(s.get("entry_allowed"))
    ok_text = "가능" if allowed else "가능(신규 매수 중단 중)"
    lines = ["", "### 오늘의 감시", "", *_table(
        ["기준일(UTC)", "감시 종목", "매수 가능" if allowed else "수량 기준 가능 (현재 신규 매수 중단)"], "lrr",
        [(s.get("universe_date"), len(universe), sum(1 for u in universe if u.get("eligible")))]),
        "", "#### 감시 종목", "", *_table(
        ["종목", "현재가", "최대 수량", "매수 가능성"], "lrrl",
        [(u.get("symbol"), _num(u.get("price"), ",.6g"), _qty(u.get("max_qty")),
          ok_text if u.get("eligible") else (u.get("reason") or "불가")) for u in universe], "감시 종목 없음"),
        "", "#### 신호 확인 단계", "", *_table(
        ["종목", "전략", "연속 확인"], "llr",
        [(p.get("symbol"), STRATEGY_KO.get(p.get("strategy"), p.get("strategy")),
          f"{int(p.get('count', 0))}/{SIGNAL_CONFIRMATIONS}") for p in s.get("signal_progress") or []],
        "진행 중인 신호 없음")]
    return lines


def _risk_section(s: dict) -> list[str]:
    return ["", "### 위험 관리", "", *_table(
        ["고점 대비 낙폭", "일일 손실 한도 기준 수익률 (오늘 첫 평가 대비)", "주문 배율", "열린 위험 합계", "남은 슬롯"],
        "rrrrr",
        [(_pct(s.get("drawdown_pct"), False), _pct(s.get("daily_return_pct")),
          _num((s.get("size_multiplier") or 0) * 100, ".0f", "%"), _pct(s.get("open_risk_pct"), False),
          s.get("free_slots"))])]


def _protective_text(p: dict) -> str:
    if p.get("protective_state") == "canceled":
        return f"{p.get('protective')} (취소 확인 중)"
    return str(p.get("protective"))


def _positions_section(s: dict) -> list[str]:
    rows = [(p.get("symbol"), _qty(p.get("qty")), _num(p.get("buy_price"), ",.6g"), _num(p.get("price"), ",.6g"),
             _pct(p.get("return_pct")), _num(p.get("initial_stop"), ",.6g"), _num(p.get("stop_price"), ",.6g"),
             _protective_text(p), _num(p.get("hours_left"), ",.1f", "시간"), "ON" if p.get("trailing_active") else "OFF")
            for p in s.get("positions") or []]
    return ["", "### 보유 포지션", "", *_table(
        ["종목", "수량", "매수가", "현재가", "수익률", "최초 손절", "현재 보호선", "거래소 손절 주문", "남은 시간", "추적"],
        "lrrrrrrlrl", rows, "보유 종목 없음")]


def _returns_section(s: dict) -> list[str]:
    ret = s.get("returns") or {}
    today = _utc_day(s.get("now"))
    rows = []
    for label, key, span in (("전일 대비", "1d", 1), ("7행 전 대비", "7d", 7), ("30행 전 대비", "30d", 30),
                             ("첫 기록 대비", "all", None)):
        r = ret.get(key) or {}
        base = r.get("base_day")
        basis = f"기준 {base} 종가" if base else "기준 -"
        if base and span and today and (today - datetime.fromisoformat(base).date()).days > span:
            basis += " · 기록 누락 있음"
        rows.append((f"{label} ({basis})", _pct(r.get("bot")), _pct(r.get("btc"))))
    start = ret.get("start") or {}
    start_label = "구간 시작 대비" if start.get("kind") == "reconcile" else "시작 자본 대비"
    rows.append((f"{start_label} ({start.get('base_day') or '-'}부터)", _pct(start.get("bot")), None))
    st = _stats(s)
    lines = ["", "## 2. 수익률", "", "### 기간별", "", *_table(["기간", "봇", "BTC 보유"], "lrr", rows),
        "", "> 수익률은 UTC 0시 평가액 기록 기준이며, 봇이 꺼져 있던 날은 기록이 없습니다. "
            "수동 정리·시작 자본 재설정 뒤에는 새 구간의 기록만 씁니다.",
        "", "### 누적 성과", "", *_table(
        ["청산", "승률", "평균 이익", "평균 손실", "Profit Factor", "최대 낙폭", "최대 연속 손실", "누적 순손익",
         "누적 수수료", "평균 슬리피지"], "rrrrrrrrrr",
        [(st["closed"], _num(st["win_rate"], ".1f", "%"), _pct(st["avg_win_pct"]), _pct(st["avg_loss_pct"]),
          _num(st["profit_factor"]), _pct(st["max_drawdown_pct"], False), f"{st['max_loss_streak']}회",
          _num(st["total_net_pnl"], "+,.2f", " USDT"), _num(st["total_fees_usdt"], ",.4f", " USDT"),
          _pct(st["avg_slippage_pct"]))]),
        "", "### 전략별", "", *_table(
        ["전략", "청산", "승률", "평균 수익률"], "lrrr",
        [(STRATEGY_KO.get(k, k), v["closed"], _num(v["win_rate"], ".1f", "%"), _pct(v["avg_return_pct"]))
         for k, v in st["by_strategy"].items()], "청산 없음"),
        "", "### 주간 평가액", "", *_table(
        ["주말(일요일, UTC)", "평가액(USDT)", "주간 수익률"], "lrr",
        [(w.get("day"), _num(w.get("equity")), _pct(w.get("return_pct"))) for w in s.get("weekly_equity") or []],
        "기록 없음"),
        "", "> 청산 1건은 매도 기록 1건입니다(부분 매도도 각각 1건). 잔량 정리는 통계에서 뺍니다. "
            "최대 낙폭은 누적 순손익 기준(시작 자본 대비)입니다.",
    ]
    if str(s.get("mode", "demo")).lower() == "demo":
        lines.append("> 데모 가격은 실거래소와 유사하지만 같지 않음.")
    if st["fees_unpriced_assets"]:
        lines.append(f"> 누적 수수료에서 뺀 자산(가격 모름): {_md(', '.join(st['fees_unpriced_assets']))}")
    return lines


def _top_halts(halt_seconds: dict) -> str:
    top = sorted(halt_seconds.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return ", ".join(f"{code} {_hours(sec)}" for code, sec in top) or "없음"


def _errors_section(s: dict) -> list[str]:
    m = s.get("metrics") or {}
    w = [m.get(k) or {} for k in ("24h", "7d", "all")]

    def line(label, fn):
        return (label, *[fn(x) for x in w])
    rows = [
        line("API 호출", lambda x: f"{x.get('api_total', 0):,}"),
        line("API 실패율", lambda x: f"{x.get('api_fail_rate', 0):.2f}% ({x.get('api_failed', 0):,}건)"),
        line("일시적/확정적 실패", lambda x: f"{x.get('api_transient', 0)} / {x.get('api_permanent', 0)}"),
        line("주문 전송/거절/미확인/부분 체결", lambda x: " / ".join(str(x.get(k, 0)) for k in (
            "orders_sent", "orders_rejected", "orders_unconfirmed", "orders_partial"))),
        line("시세 재연결", lambda x: f"{x.get('feed_reconnects', 0)}회"),
        line("시세 끊김 시간", lambda x: _hours(x.get("feed_down_seconds"))),
        line("가동률", lambda x: f"{x.get('uptime_pct', 0):.2f}%"),
        line("중단 시간(사유별, 상위 5)", lambda x: _top_halts(x.get("halt_seconds") or {})),
        line("봇 꺼짐 시간", lambda x: _hours(x.get("offline_seconds"))),
        line("재시작", lambda x: f"{x.get('restarts', 0)}회"),
        line("루프 예외", lambda x: f"{x.get('loops_failed', 0)}회"),
    ]
    now = _utc(s.get("now"))
    minutes = (now.hour * 60 + now.minute) if now else None
    cover = f"{minutes // 60}시간 {minutes % 60}분" if minutes is not None else "-"
    return ["", "## 3. 오류율", "", *_table(["항목", "오늘(UTC 0시~)", "7일", "전체"], "lrrr", rows),
            "", f"> 오늘 열은 UTC 0시부터 {cover} 분량입니다. 7일은 오늘 포함 7일(UTC 날짜)입니다."]


def _events_section(s: dict) -> list[str]:
    events = sorted((e for e in s.get("events") or [] if not str(e.get("type", "")).startswith("_")),
                    key=lambda e: str(e.get("timestamp", "")), reverse=True)
    shown = events[:_MAX_EVENT_ROWS]
    rows = [(_kst(e.get("timestamp")), e.get("type"), e.get("symbol"), _qty(e.get("qty")),
             _num(e.get("price"), ",.6g"), e.get("reason"), int(e.get("count", 1))) for e in shown]
    return ["", f"## 4. 최근 7일 이벤트 (총 {len(events)}건, 최근 {len(shown)}건 표시)", "", *_table(
        ["시각", "유형", "종목", "수량", "가격", "사유", "반복"], "lllrrlr", rows, "기록 없음")]


def render_dashboard(snapshot: dict) -> str:
    s = snapshot
    lines = [f"# Coin Sniper Dashboard  [{str(s.get('mode', 'demo')).upper()}]", "",
             f"> 마지막 갱신: **{_kst(s.get('now')) or '-'} KST**", ""]
    lines += _status_section(s) + _watch_section(s) + _risk_section(s) + _positions_section(s)
    lines += _returns_section(s) + _errors_section(s) + _events_section(s)
    lines += ["", "> 인증정보와 계좌 식별정보는 기록하지 않습니다.", ""]
    return "\n".join(lines)


# ---- 상태 JSON ----------------------------------------------------------------------------
def _clean(value):
    if isinstance(value, float):
        return round(value, 8)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def build_state_json(snapshot: dict) -> dict:
    """원시 trades·events를 뺀 전부 + performance_stats. 이벤트는 따로 dashboard_events.json으로 게시한다."""
    state = {k: v for k, v in snapshot.items() if k not in ("trades", "events")}
    state["performance_stats"] = _stats(snapshot)
    return _clean(state)


# ---- 게시 --------------------------------------------------------------------------------
def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def publish(readme: str, state: dict, events: list[dict], repo_dir: Path, work_dir: Path,
            runner: Callable = subprocess.run) -> bool:
    """세 파일을 부모 없는 단일 커밋으로 origin/dashboard에 덮어쓴다(원본 sync_dashboard_to_branch와 같은 절차).
    어떤 실패든 False만 돌려준다: git의 출력(stderr)은 어디에도 남기지 않는다."""
    try:
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        files = {"README.md": readme,
                 "dashboard_events.json": json.dumps(events, ensure_ascii=False, indent=2),
                 "dashboard_state.json": json.dumps(state, ensure_ascii=False, indent=2)}

        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")   # 자격 증명 프롬프트로 멈추지 않는다

        def git(*args, **extra) -> str:
            if "input" not in extra:                       # mktree는 input이 표준 입력(파이프)이다
                extra["stdin"] = subprocess.DEVNULL
            return runner(["git", *args], cwd=repo_dir, capture_output=True, text=True, check=True,
                          timeout=DASHBOARD_PUBLISH_TIMEOUT_SECONDS, env=env, **extra).stdout.strip()
        tree_input = ""
        for name, text in files.items():           # 이름 순서(git 트리 정렬과 같다)
            _write_atomic(work_dir / name, text)
            blob = git("hash-object", "-w", str(work_dir / name))
            tree_input += f"100644 blob {blob}\t{name}\n"
        tree = git("mktree", input=tree_input)
        commit = git("commit-tree", tree, "-m", COMMIT_MESSAGE)
        git("push", "-f", "origin", f"{commit}:refs/heads/dashboard")
        return True
    except Exception:                              # git 없음, 비정상 종료, 시간 초과, 파일 쓰기 실패 모두
        return False


class PublishSchedule:
    """체결·위험·오류(critical)는 즉시, 변화가 있거나 보유 중이면 5분, 변화가 없어도 30분마다.
    게시가 실패하면(failed) 그때부터 5분은 critical이어도 다시 시도하지 않는다. 플래그는 남겨 둔다."""

    def __init__(self):
        self._last: float | None = None
        self._failed_at: float | None = None
        self._dirty = False
        self._critical = False

    def mark_dirty(self, critical: bool = False) -> None:
        self._dirty = True
        self._critical = self._critical or critical

    def failed(self, now_ts: float) -> None:
        self._failed_at = now_ts

    def due(self, now_ts: float, has_positions: bool) -> bool:
        if self._failed_at is not None and now_ts - self._failed_at < DASHBOARD_MIN_INTERVAL_SECONDS:
            return False
        if self._last is None or self._critical:
            return True
        elapsed = now_ts - self._last
        if elapsed >= DASHBOARD_HEARTBEAT_SECONDS:
            return True
        return (self._dirty or has_positions) and elapsed >= DASHBOARD_MIN_INTERVAL_SECONDS

    def published(self, now_ts: float) -> None:
        self._last = now_ts
        self._failed_at = None
        self._dirty = self._critical = False
