"""코인 봇(바이낸스 현물) 실행 명령.

    python coin_bot.py run                              봇 실행(전경, Ctrl+C로 중지)
    python coin_bot.py reconcile [--reset-capital] [--yes]   거래소에 없는 장부 포지션 수동 정리
    python coin_bot.py status                           저장된 상태 요약(키·네트워크 불필요)

매매 판단은 전부 엔진(coin_sniper.engine)에 있다. 여기서는 연결하고, 돌리고, 출력하고, 대시보드를 게시한다.
키와 시크릿은 어디에도 출력하지 않는다. 주식 봇(bot.py)과는 별개 프로그램이다.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from env_loader import load_dotenv
from coin_sniper.config import POLL_INTERVAL_SECONDS, QUOTE_ASSET, Settings, SettingsError, load_settings
from coin_sniper.dashboard import STATUS_KO, PublishSchedule, build_state_json, publish, render_dashboard
from coin_sniper.engine import Engine
from coin_sniper.exchange.base import Exchange
from coin_sniper.exchange.binance import BinanceExchange
from coin_sniper.exchange.binance_feed import BinanceFeed
from coin_sniper.ledger import AlreadyRunningError, InstanceLock, StateCorruptError, StateStore, record_event
from coin_sniper.metrics import Metrics

REPO_ROOT = Path(__file__).resolve().parent
LOCK_NAME = "coin_bot.lock"
KST = timezone(timedelta(hours=9), "KST")
COMMIT_TIMEOUT_SECONDS = 10
CRITICAL_EVENT_TYPES = frozenset({"BUY", "SELL", "RISK", "ERROR"})
MANUAL_REASON = "수동 정리"


class ReconcileError(RuntimeError):
    """수동 정리를 할 수 없다(장부는 그대로)."""


class ReconcileAborted(RuntimeError):
    """사람이 확인을 거절했다(장부는 그대로)."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _kst(value: datetime | str | None) -> str:
    if not value:
        return "-"
    try:
        t = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        return str(value)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


def read_commit(runner: Callable = subprocess.run) -> str:
    """실행 중인 코드의 짧은 커밋 해시. 어떤 실패든 "-"."""
    try:
        out = runner(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True,
                     check=True, timeout=COMMIT_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
        return str(out.stdout).strip() or "-"
    except Exception:
        return "-"


# ---- run ------------------------------------------------------------------------------
def _banner(settings: Settings) -> None:
    print("=" * 60)
    print(f"Coin Sniper 시작  [{settings.mode.upper()}]")
    if settings.mode == "live":
        print("!" * 60)
        print("!! 실거래 모드입니다. 실제 자금으로 실제 주문이 나갑니다.")
        if not (settings.state_dir / "risk_state.json").exists():   # 잠금 파일 때문에 폴더는 이미 있을 수 있다
            print("!! 첫 실행입니다: 실거래 상태 폴더가 아직 없습니다.")
        print("!! 멈추려면 Ctrl+C. 보유 포지션의 거래소 손절 주문은 남습니다.")
        print("!" * 60)
    else:
        print("데모 모드: 바이낸스 데모 환경에 주문합니다(실제 자금 아님).")
    print(f"REST: {settings.rest_base}")
    print(f"시세: {settings.stream_base}")
    print(f"상태 폴더: {settings.state_dir}")
    print(f"시작 자본(가상 계좌): {settings.starting_capital:,.2f} {QUOTE_ASSET}")
    print(f"대시보드 게시(AUTO_GIT_PUSH): {'켜짐' if settings.auto_git_push else '꺼짐'}")
    print(f"루프 주기: {POLL_INTERVAL_SECONDS}초")
    print("=" * 60)


def _status_line(report, positions: int) -> str:
    if not report.ok:
        status = "error"
    else:
        status = "halted" if report.halts else "running"
    equity = f"{report.equity:,.2f} {QUOTE_ASSET}" if report.equity is not None else "-"
    codes = ",".join(h["code"] for h in report.halts) or "없음"
    entry = "가능" if report.entry_allowed else "중단"
    return (f"{_kst(report.now)} KST | {STATUS_KO.get(status, status)} | 평가액 {equity} | 보유 {positions} | "
            f"중단: {codes} | 신규 매수 {entry}")


def _event_line(event: dict) -> str:
    parts = [f"  - [{event.get('type', '')}]"]
    if event.get("symbol"):
        parts.append(str(event["symbol"]))
    parts.append(str(event.get("reason", "")))
    extra = []
    if event.get("qty") is not None:
        extra.append(f"수량 {event['qty']}")
    if event.get("price") is not None:
        extra.append(f"가격 {event['price']}")
    if extra:
        parts.append("(" + ", ".join(extra) + ")")
    return " ".join(parts)


def _maybe_publish(engine: Engine, settings: Settings, schedule: PublishSchedule, now: datetime,
                   publish_fn: Callable, now_fn: Callable[[], datetime]) -> None:
    if not schedule.due(now.timestamp(), bool(engine.positions)):
        return
    try:
        snap = engine.snapshot(now)
        ok = publish_fn(render_dashboard(snap), build_state_json(snap), snap["events"],
                        REPO_ROOT, settings.state_dir / "dashboard")
    except Exception as exc:                              # 게시 문제는 매매를 멈추지 않는다
        schedule.failed(now_fn().timestamp())
        print(f"대시보드 게시 오류(매매는 계속): {type(exc).__name__}")
        return
    if ok:
        schedule.published(now_fn().timestamp())
    else:
        schedule.failed(now_fn().timestamp())
        print("대시보드 게시 실패(매매는 계속)")


def run(settings: Settings, *, exchange: Exchange | None = None, feed=None, sleep: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] = utc_now, clock: Callable[[], float] = time.monotonic,
        max_loops: int | None = None, publish_fn: Callable = publish, commit_fn: Callable[[], str] = read_commit,
        schedule: PublishSchedule | None = None) -> int:
    """봇 루프. 잠금은 호출자(main)가 잡는다. 반환값은 종료 코드."""
    _banner(settings)
    store = StateStore(settings.state_dir)
    try:
        metrics = Metrics(store.load("metrics", None))
    except StateCorruptError as exc:
        print(f"오류: {exc}. 파일을 확인하고 다시 실행하세요.", file=sys.stderr)
        return 2
    if exchange is None:
        # 어댑터는 이 메인 스레드에서만 불린다: 지표 기록이 안전하다.
        exchange = BinanceExchange(settings, on_call=lambda endpoint, result: metrics.record_api(
            endpoint, result, now_fn()))
    if feed is None:
        feed = BinanceFeed(settings)                      # 재연결 횟수는 루프에서 꺼내 기록한다(지표는 스레드 안전하지 않다)
    engine = Engine(exchange, feed, store, metrics, settings, sleep=sleep)
    engine.commit = commit_fn()
    print(f"실행 커밋: {engine.commit}")
    try:
        engine.startup(now_fn())
    except StateCorruptError as exc:
        print(f"오류: {exc}. 빈 장부로 시작하지 않습니다. 파일을 확인하고 다시 실행하세요.", file=sys.stderr)
        return 2
    schedule = schedule if schedule is not None else PublishSchedule()
    take_reconnects = getattr(feed, "take_reconnect_count", None)
    prev_codes: set[str] | None = None
    loops = 0
    try:
        while True:
            started = clock()
            now = now_fn()
            report = engine.run_once(now)
            reconnects = take_reconnects() if take_reconnects is not None else 0
            if reconnects:
                for _ in range(reconnects):
                    metrics.record_feed_reconnect(now)
                try:
                    store.save("metrics", metrics.to_dict())
                except OSError as exc:
                    print(f"지표 저장 실패: {type(exc).__name__}")
            print(_status_line(report, len(engine.positions)), flush=True)
            for event in report.events:
                print(_event_line(event), flush=True)
            codes = {h["code"] for h in report.halts}
            if settings.auto_git_push:
                changed = prev_codes is not None and codes != prev_codes
                if report.events or changed:
                    critical = changed or any(e.get("type") in CRITICAL_EVENT_TYPES for e in report.events)
                    schedule.mark_dirty(critical=critical)
                _maybe_publish(engine, settings, schedule, now, publish_fn, now_fn)
            prev_codes = codes
            loops += 1
            if max_loops is not None and loops >= max_loops:
                break
            sleep(max(0.0, POLL_INTERVAL_SECONDS - (clock() - started)))
    except KeyboardInterrupt:
        print("중지합니다")
    return 0


# ---- reconcile ------------------------------------------------------------------------
def _base_asset(symbol: str, rules: dict) -> str:
    rule = rules.get(symbol)
    if rule is not None:
        return rule.base
    return symbol[:-len(QUOTE_ASSET)] if symbol.endswith(QUOTE_ASSET) else symbol


def _dec(value) -> Decimal:
    return Decimal(repr(float(value)))


def reconcile(store: StateStore, exchange: Exchange, now: datetime, reset_capital: bool, *,
              confirm: Callable[[str], bool] | None = None, starting_capital: float | None = None) -> list[str]:
    """거래소에 없는 장부 포지션을 "수동 정리"로 닫는다(통계 제외, 원가는 실현 손익에서 뺀다). 정리한 심볼을 돌려준다.

    confirm이 있으면 계획을 보여 준 뒤 물어보고, 거절하면 ReconcileAborted. 할 수 없으면 ReconcileError.
    어느 경우든 예외면 장부는 그대로다. starting_capital을 주면 재설정 때 시작 자본을 그 값으로 바꾼다."""
    positions = store.load("positions", {})
    risk_state = store.load("risk_state", {})
    intents = store.load("intents", {})
    trades = store.load("trades", [])
    events = store.load("events", [])
    if not risk_state and not positions:
        print("아직 실행 기록이 없습니다. 정리할 것이 없습니다.")
        return []

    res = exchange.balances()
    if not res.ok:
        raise ReconcileError(f"계좌 조회 실패: {res.error}. 장부는 바꾸지 않았습니다")
    balances = dict(res.value)
    got = exchange.symbol_rules()
    if got.ok:
        rules = dict(got.value)
    else:
        rules = {}
        print(f"종목 규칙 조회 실패({got.error}): 허용 오차 0으로 판정합니다")

    print("수동 정리 계획 (장부 수량 / 거래소 사용 가능+잠김 수량):")
    removed, kept = [], []
    for symbol in sorted(positions):
        pos = positions[symbol]
        rule = rules.get(symbol)
        free, locked = balances.get(_base_asset(symbol, rules), (0.0, 0.0))
        held = _dec(free) + _dec(locked)
        need = _dec(pos["qty"]) - (_dec(rule.step_size) if rule else Decimal(0))
        missing = held < need
        (removed if missing else kept).append(symbol)
        action = f"정리(장부에서 삭제, 원가 {float(pos['cost']):,.2f} {QUOTE_ASSET} 손실 처리)" if missing else "유지"
        print(f"  {symbol}: 장부 {pos['qty']} / 거래소 {float(held)} -> {action}")
    if not positions:
        print("  (장부에 포지션 없음)")

    if reset_capital and kept:
        raise ReconcileError("포지션이 남아 있어 시작 자본을 재설정할 수 없습니다(원가가 이중으로 계산됩니다): "
                             + ", ".join(kept))
    if not removed and not reset_capital:
        print("정리할 포지션이 없습니다. 장부는 그대로입니다.")
        return []
    capital = float(starting_capital if starting_capital is not None else risk_state.get("starting_capital", 0.0))
    if reset_capital:
        print(f"시작 자본 재설정: 실현 손익 0, 시작 자본·고점 {capital:,.2f} {QUOTE_ASSET}, 수익률 새 구간 시작")
    elif removed:
        print("수익률은 지금부터 새 구간으로 계산합니다(시작 자본은 그대로).")
    dropped = sorted(cid for cid, i in intents.items() if i.get("symbol") in removed)
    if dropped:
        print(f"함께 지울 진행 중 주문 기록: {', '.join(dropped)}")
    if confirm is not None and not confirm("진행할까요? [y/N] "):
        raise ReconcileAborted("취소했습니다. 장부는 그대로입니다")

    realized = float(risk_state.get("realized_pnl", 0.0))
    for symbol in removed:
        pos = positions.pop(symbol)
        cost = float(pos["cost"])
        trades.append({
            "time": now.isoformat(), "type": "SELL", "symbol": symbol, "qty": float(pos["qty"]), "price": 0,
            "quote_qty": 0, "fees": {}, "strategy": pos.get("strategy"), "reason": MANUAL_REASON,
            "client_id": f"manual-{uuid.uuid4().hex[:12]}", "entry_price": float(pos["buy_price"]),
            "gross_pnl": 0, "net_pnl": 0, "net_return_pct": 0, "slippage_pct": None, "counted": False,
            "dust_writeoff": cost,
        })
        realized -= cost                                  # 사라진 돈: 가상 현금이 늘지 않게 원가를 실현 손익에서 뺀다
    intents = {cid: i for cid, i in intents.items() if cid not in dropped}
    risk_state["realized_pnl"] = realized
    if removed or reset_capital:
        risk_state["segment_start"] = now.isoformat()
    if reset_capital:
        risk_state.update(starting_capital=capital, realized_pnl=0.0, peak_equity=capital,
                          day_open_equity=None, pending_equity_row=None)
    reasons = [f"{MANUAL_REASON}: {', '.join(removed)}"] if removed else [MANUAL_REASON]
    if reset_capital:
        reasons.append("시작 자본 재설정")
    events = record_event(events, {"type": "SYSTEM", "symbol": "", "reason": "; ".join(reasons),
                                   "timestamp": now.isoformat(timespec="seconds")}, now)

    store.save("trades", trades)
    store.save("risk_state", risk_state)
    store.save("positions", positions)
    store.save("intents", intents)
    store.save("events", events)
    print("완료: " + "; ".join(reasons))
    return removed


# ---- status ---------------------------------------------------------------------------
def status(settings: Settings) -> int:
    store = StateStore(settings.state_dir)
    print(f"모드: {settings.mode.upper()}  (상태 폴더 {settings.state_dir})")
    if not settings.state_dir.exists():
        print("아직 실행 기록이 없습니다")
        return 0
    try:
        positions = store.load("positions", {})
        risk_state = store.load("risk_state", {})
        halts = store.load("halts", {}).get("halts", [])
        intents = store.load("intents", {})
    except StateCorruptError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(f"마지막 정상 루프: {_kst(risk_state.get('last_loop_at'))} KST")
    capital = float(risk_state.get("starting_capital", 0.0) or 0.0)
    realized = float(risk_state.get("realized_pnl", 0.0) or 0.0)
    cash = capital + realized - sum(float(p.get("cost", 0.0)) for p in positions.values())
    print(f"시작 자본 {capital:,.2f} / 실현 손익 {realized:+,.2f} / 가상 현금 {cash:,.2f} {QUOTE_ASSET}"
          f" (구간 시작 {_kst(risk_state.get('segment_start'))} KST)")
    print(f"보유 포지션 {len(positions)}개")
    for symbol, pos in sorted(positions.items()):
        print(f"  {symbol}: 수량 {pos.get('qty')} / 매수가 {float(pos.get('buy_price', 0)):,.6g}"
              f" / 보호선 {float(pos.get('stop_price', 0)):,.6g} / 진입 {_kst(pos.get('entry_time'))} KST")
    print(f"중단 사유 {len(halts)}개" + ("" if halts else " (신규 매수는 다음 루프 판단에 따름)"))
    for h in halts:
        print(f"  {h.get('code')}: {h.get('detail')} (시작 {_kst(h.get('since'))} KST)")
    print(f"진행 중 주문 기록 {len(intents)}개")
    for cid, intent in sorted(intents.items()):
        print(f"  {intent.get('symbol')} {intent.get('side')} {cid}")
    return 0


# ---- main -----------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python coin_bot.py", description="Coin Sniper (바이낸스 현물 봇)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="봇 실행(전경)")
    rec = sub.add_parser("reconcile", help="거래소에 없는 장부 포지션을 수동 정리")
    rec.add_argument("--reset-capital", action="store_true", help="실현 손익을 0으로, 시작 자본을 설정값으로 되돌린다")
    rec.add_argument("--yes", action="store_true", help="확인 질문 없이 진행")
    sub.add_parser("status", help="저장된 상태 요약(키·네트워크 불필요)")
    return parser


def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() == "y"
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    load_dotenv(str(REPO_ROOT / ".env"))
    try:
        settings = load_settings(os.environ)
        if args.command in ("run", "reconcile"):
            settings.require_keys()
    except SettingsError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    if args.command == "status":
        return status(settings)

    lock = InstanceLock(settings.state_dir / LOCK_NAME)
    try:
        lock.acquire()
    except AlreadyRunningError:
        print(f"이미 실행 중입니다 ({settings.mode} 모드). 봇을 멈춘 뒤 다시 실행하세요.", file=sys.stderr)
        return 2
    try:
        if args.command == "run":
            return run(settings)
        exchange = BinanceExchange(settings)
        try:
            reconcile(StateStore(settings.state_dir), exchange, utc_now(), args.reset_capital,
                      confirm=None if args.yes else _ask, starting_capital=settings.starting_capital)
        except StateCorruptError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 2
        except (ReconcileError, ReconcileAborted) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    except KeyboardInterrupt:
        print("중지합니다")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
