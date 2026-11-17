"""데모 모드 확인 스크립트: python -m coin_sniper.probe

사람이 자기 데모 API 키로 한 번 실행해 데모 환경이 실제로 무엇을 지원하는지 확인한다.
- MODE=demo가 아니면 실행을 거부한다(종료 코드 2). 키는 어디에도 출력하지 않는다.
- 일곱 항목을 순서대로 실행하며 한 항목이 실패해도 나머지를 계속한다.
- 거래는 BTCUSDT 소액 매수 1회와 그 수량의 매도뿐이다. 다른 자산은 건드리지 않는다.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import env_loader
from coin_sniper.config import (
    CANDLE_COUNT, MIN_HISTORY_DAYS, MIN_PREV_DAY_QUOTE_VOLUME, QUOTE_ASSET, UNIVERSE_MIN_COUNT,
    UNIVERSE_TOP_N, Settings, SettingsError, load_settings,
)
from coin_sniper.exchange.binance import BinanceExchange
from coin_sniper.exchange.binance_feed import BinanceFeed
from coin_sniper.risk import floor_to_step, floor_to_tick
from coin_sniper.universe import is_excluded, select_universe

# 이 확인 스크립트에만 쓰는 수치(전략 파라미터가 아니다)
PROBE_SYMBOL = "BTCUSDT"
PROBE_NOTIONAL_MULTIPLE = 3        # 최소 주문 금액의 3배만 매수
PROBE_STOP_FACTOR = 0.90           # 손절 발동가 = 현재가 -10%
PROBE_STOP_LIMIT_FACTOR = 0.99     # 손절 지정가 = 발동가 -1%
STREAM_WAIT_SECONDS = 10           # 시세 스트림을 기다리는 시간(초)
BALANCE_LIST_CAP = 30
CANDLE_PAUSE_SECONDS = 0.1
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "docs" / "superpowers" / "specs" / "2026-11-17-demo-probe-results.md"
_KST = timezone(timedelta(hours=9))

TITLES = {
    1: "서버 시각과 계좌 조회",
    2: "초기 잔고 구성",
    3: "감시 종목 후보 수",
    4: "시장가 매수",
    5: "손절 주문 등록·조회·취소",
    6: "전량 시장가 매도",
    7: "시세 스트림",
}


class _Skip(Exception):
    pass


class _Fail(Exception):
    pass


def _need(result, what: str):
    if not result.ok:
        raise _Fail(f"{what} 실패 ({result.kind}): {result.error}")
    return result.value


def _hex12() -> str:
    return uuid.uuid4().hex[:12]


def _fees_text(fees: dict) -> str:
    return ", ".join(f"{a} {v:.10g}" for a, v in fees.items()) or "없음"


def _now_utc(clock) -> datetime:
    value = clock()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


# ---- 개별 확인 ---------------------------------------------------------------------
def _check_account(ex, feed, sleep, clock, ctx) -> tuple[str, str]:
    server_ms = _need(ex.server_time(), "server_time")
    balances = _need(ex.balances(), "balances")
    when = datetime.fromtimestamp(server_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    drift_ms = int(server_ms - _now_utc(clock).timestamp() * 1000)
    return "PASS", f"서버 시각 {when} UTC (로컬과 차이 {drift_ms} ms), 보유 자산 {len(balances)}종"


def _check_balances(ex, feed, sleep, clock, ctx) -> tuple[str, str]:
    balances = _need(ex.balances(), "balances")
    held = sorted((a, f + l) for a, (f, l) in balances.items() if f + l != 0)
    shown = ", ".join(f"{a} {total:.10g}" for a, total in held[:BALANCE_LIST_CAP])
    more = f" 외 {len(held) - BALANCE_LIST_CAP}종" if len(held) > BALANCE_LIST_CAP else ""
    return "PASS", f"잔고가 있는 자산 {len(held)}종: {shown or '없음'}{more}"


def _check_universe(ex, feed, sleep, clock, ctx) -> tuple[str, str]:
    rules = _need(ex.symbol_rules(), "symbol_rules")
    tradable = [s for s, r in rules.items() if r.quote == QUOTE_ASSET and r.trading]
    known_bases = frozenset(r.base for r in rules.values())
    candidates = [s for s in tradable if not is_excluded(rules[s], known_bases)]
    candles: dict[str, list[dict]] = {}
    failed = 0
    for symbol in candidates:
        result = ex.daily_candles(symbol, CANDLE_COUNT)
        if result.ok:
            candles[symbol] = result.value
        else:
            failed += 1
        sleep(CANDLE_PAUSE_SECONDS)
    qualifying = [s for s, c in candles.items()
                  if len(c) >= max(2, MIN_HISTORY_DAYS) and c[-2]["quote_vol"] >= MIN_PREV_DAY_QUOTE_VOLUME]
    selected = select_universe(rules, candles)
    status = "PASS" if len(qualifying) >= UNIVERSE_MIN_COUNT else "FAIL"
    text = (f"{QUOTE_ASSET} 거래 가능 {len(tradable)}종, 제외 후 후보 {len(candidates)}종, "
            f"일봉 {MIN_HISTORY_DAYS}개 이상·전일 거래대금 {MIN_PREV_DAY_QUOTE_VOLUME:,} 이상 {len(qualifying)}종 "
            f"(select_universe 결과 {len(selected)}종, 상한 {UNIVERSE_TOP_N}), 기준 {UNIVERSE_MIN_COUNT}종")
    if failed:
        text += f", 일봉 조회 실패 {failed}종"
    return status, text


BUY_LOOKUP_ATTEMPTS = 10          # 매수 결과가 불분명할 때 get_order를 초당 1회씩 다시 보는 횟수


def _lookup_order(ex, sleep, client_id: str, attempts: int):
    """찾았고 최종 상태가 될 때까지 조회한다. 마지막으로 얻은 상태(없으면 None)를 돌려준다."""
    state = None
    for i in range(attempts):
        result = ex.get_order(PROBE_SYMBOL, client_id)
        if result.ok and result.value.exists:
            state = result.value
            if state.is_final:
                break
        if i < attempts - 1:
            sleep(1)
    return state


def _record_buy(ctx, rule, state, estimate_price) -> float:
    received = state.executed_qty - state.fees.get(rule.base, 0.0)
    ctx.update(rule=rule, price=state.avg_price or estimate_price, received=received,
               bought=state.executed_qty, unsold=floor_to_step(received, rule.step_size))
    return received


def _check_buy(ex, feed, sleep, clock, ctx) -> tuple[str, str]:
    rules = _need(ex.symbol_rules(), "symbol_rules")
    rule = rules.get(PROBE_SYMBOL)
    if rule is None:
        raise _Fail(f"{PROBE_SYMBOL} 규칙이 없음")
    candles = _need(ex.daily_candles(PROBE_SYMBOL, 2), "daily_candles")
    if not candles:
        raise _Fail("일봉이 비어 있음")
    price = float(candles[-1]["close"])
    qty = floor_to_step(PROBE_NOTIONAL_MULTIPLE * rule.min_notional / price, rule.step_size)
    if qty * price < rule.min_notional:
        qty = floor_to_step(qty + rule.step_size, rule.step_size)
    client_id = f"probe-buy-{_hex12()}"
    ctx["buy_id"] = client_id          # 전송 전에 기억한다: 응답을 잃어도 조회할 수 있어야 한다
    ctx["rule"] = rule
    sent = ex.market_buy(PROBE_SYMBOL, qty, client_id)
    if not sent.ok and sent.kind == "permanent":       # 거래소가 명시적으로 거절: 주문이 없다
        ctx.pop("buy_id")
        raise _Fail(f"market_buy 실패 ({sent.kind}): {sent.error}")
    state = _lookup_order(ex, sleep, client_id, BUY_LOOKUP_ATTEMPTS)
    if state is None or (not state.is_final and state.executed_qty <= 0):
        ctx["buy_unknown"] = True                      # 접수만 됐을 수 있다: 6번이 확인된 수량만 다시 본다
        raise _Fail(f"매수 결과를 확인하지 못함 (client id {client_id}, 전송 응답: "
                    f"{'ok' if sent.ok else sent.error}). 데모 계정에 BTC가 남았을 수 있음")
    if state.executed_qty <= 0:
        ctx.pop("buy_id")
        raise _Fail(f"체결되지 않음: 상태 {state.status}, 체결 수량 {state.executed_qty} ({client_id})")
    received = _record_buy(ctx, rule, state, price)
    text = (f"요청 {qty:.10g} {rule.base}, 체결 {state.executed_qty:.10g}, 평균가 {state.avg_price:.10g}, "
            f"수수료 {_fees_text(state.fees)}, 실제 받은 수량 {received:.10g} {rule.base}, "
            f"최소 주문 금액 {rule.min_notional:g}, 상태 {state.status}")
    if not sent.ok:
        raise _Fail(f"전송 응답은 실패({sent.kind}: {sent.error})였지만 주문은 체결됨 ({client_id}). {text}")
    return "PASS", text


def _cancel_stop(ex, ctx) -> str:
    """남아 있을 수 있는 손절 주문을 취소하고 사라졌는지 확인한다. 실패하면 _Fail."""
    stop_id = ctx["stop_id"]
    cancel = ex.cancel_order(PROBE_SYMBOL, stop_id)
    after = _need(ex.open_orders(PROBE_SYMBOL), "open_orders")
    if any(o.client_id == stop_id for o in after):
        raise _Fail(f"취소 후에도 미체결에 남아 있음 (취소 응답: {'ok' if cancel.ok else cancel.error})")
    ctx["stop_id"] = None
    return "ok" if cancel.ok else f"취소 응답 오류({cancel.error})지만 미체결에서 사라짐"


def _check_stop(ex, feed, sleep, clock, ctx) -> tuple[str, str]:
    if "received" not in ctx:
        raise _Skip("4번 매수가 성공하지 못해 보호할 수량이 없음")
    rule, price = ctx["rule"], ctx["price"]
    qty = floor_to_step(ctx["received"], rule.step_size)
    stop = floor_to_tick(price * PROBE_STOP_FACTOR, rule.tick_size)
    limit = floor_to_tick(stop * PROBE_STOP_LIMIT_FACTOR, rule.tick_size)
    client_id = f"probe-stop-{_hex12()}"
    ctx["stop_id"] = client_id         # 전송 전에 기억한다
    try:
        placed = ex.place_stop(PROBE_SYMBOL, qty, stop, limit, client_id)
        if not placed.ok and placed.kind == "permanent":
            ctx["stop_id"] = None      # 명시적 거절: 주문이 없다
            raise _Fail(f"place_stop 실패 ({placed.kind}): {placed.error}")
        if not placed.ok:
            raise _Fail(f"place_stop 결과 불명 ({placed.kind}): {placed.error}")
        open_now = _need(ex.open_orders(PROBE_SYMBOL), "open_orders")
        if not any(o.client_id == client_id for o in open_now):
            raise _Fail("등록은 됐지만 미체결 조회에 보이지 않음")
        cancelled = _cancel_stop(ex, ctx)
    except _Fail as exc:
        if ctx.get("stop_id"):
            raise _Fail(f"{exc} (손절 주문 {client_id}이 열려 있을 수 있음)") from None
        raise
    return "PASS", (f"STOP_LOSS_LIMIT 매도 수락 (수량 {qty:.10g}, 발동가 {stop:.10g}, 지정가 {limit:.10g}, "
                    f"등록 직후 상태 {placed.value.status}), 미체결 조회에 보임, 취소 {cancelled}, 취소 후 사라짐")


def _check_sell(ex, feed, sleep, clock, ctx) -> tuple[str, str]:
    if "buy_id" not in ctx:
        raise _Skip("4번 매수가 주문되지 않아 팔 수량이 없음")
    rule = ctx["rule"]
    notes: list[str] = []

    def fail(message: str):
        raise _Fail(message + (" / " + "; ".join(notes) if notes else ""))

    if ctx.get("stop_id"):          # 5번이 중간에 실패해 손절 주문이 남았을 수 있다
        try:
            notes.append(f"남은 손절 주문 {ctx['stop_id']} 취소 재시도: {_cancel_stop(ex, ctx)}")
        except _Fail as exc:
            notes.append(f"남은 손절 주문 {ctx['stop_id']} 취소 실패: {exc}")
    if "received" not in ctx:       # 매수 결과 불명: 한 번 더 조회하고, 확인된 수량만 판다
        result = ex.get_order(PROBE_SYMBOL, ctx["buy_id"])
        if result.ok and result.value.exists and result.value.executed_qty > 0:
            _record_buy(ctx, rule, result.value, 0.0)
            ctx.pop("buy_unknown", None)
            notes.append(f"매수 {ctx['buy_id']}가 뒤늦게 확인됨")
        else:
            bal = ex.balances()
            seen = f"BTC 잔고 {bal.value.get(rule.base)}" if bal.ok else f"잔고 조회 실패: {bal.error}"
            fail(f"매수 {ctx['buy_id']}를 확인하지 못해 매도하지 않음 ({seen}). BTC가 남았을 수 있음")
    got = ex.balances()
    if not got.ok:
        fail(f"balances 실패 ({got.kind}): {got.error}")
    free = got.value.get(rule.base, (0.0, 0.0))[0]
    qty = floor_to_step(min(ctx["received"], free), rule.step_size)    # 이 확인이 산 수량을 넘기지 않는다
    if qty <= 0:
        fail(f"팔 수량이 없음 (받은 수량 {ctx['received']:.10g}, 가용 {free:.10g})")
    client_id = f"probe-sell-{_hex12()}"
    sent = ex.market_sell(PROBE_SYMBOL, qty, client_id)
    if not sent.ok and sent.kind == "permanent":
        fail(f"market_sell 실패 ({sent.kind}): {sent.error}")
    state = _lookup_order(ex, sleep, client_id, BUY_LOOKUP_ATTEMPTS)
    if state is None:
        fail(f"매도 결과를 확인하지 못함 (client id {client_id}, 전송 응답: "
             f"{'ok' if sent.ok else sent.error})")
    ctx["unsold"] = max(0.0, qty - state.executed_qty)
    text = (f"요청 {qty:.10g}, 체결 {state.executed_qty:.10g}, 수수료 {_fees_text(state.fees)}, "
            f"상태 {state.status}" + (" / " + "; ".join(notes) if notes else ""))
    if state.status != "FILLED" or not sent.ok:
        raise _Fail(text)
    return "PASS", text


def _is_valid_quote(quote, today: str) -> bool:
    return min(quote.open, quote.high, quote.low, quote.price) > 0 and quote.day == today


def _check_stream(ex, feed, sleep, clock, ctx) -> tuple[str, str]:
    feed.set_symbols([PROBE_SYMBOL])
    feed.start()
    today = _now_utc(clock).strftime("%Y-%m-%d")
    last = None
    for i in range(STREAM_WAIT_SECONDS):
        quote = feed.quote(PROBE_SYMBOL)
        if quote is not None:
            last = quote
            if _is_valid_quote(quote, today):
                return "PASS", (f"시가 {quote.open:.10g}, 고가 {quote.high:.10g}, 저가 {quote.low:.10g}, "
                                f"현재가 {quote.price:.10g}, 거래량 {quote.volume:.10g}, 봉 날짜 {quote.day}")
        if i < STREAM_WAIT_SECONDS - 1:
            sleep(1)
    connected, error = feed.health()
    seen = "시세 없음" if last is None else (
        f"마지막 시세 시가 {last.open:.10g}, 고가 {last.high:.10g}, 저가 {last.low:.10g}, "
        f"현재가 {last.price:.10g}, 봉 날짜 {last.day} (기대 {today})")
    return "FAIL", (f"{STREAM_WAIT_SECONDS}초 안에 유효한 시세가 오지 않음: {seen}; "
                    f"연결 {'됨' if connected else '안 됨'}, {error or '오류 없음'}")


_CHECKS: list[Callable] = [_check_account, _check_balances, _check_universe, _check_buy,
                           _check_stop, _check_sell, _check_stream]


def run_probe(exchange, feed, sleep: Callable[[float], None], clock: Callable) -> list[dict]:
    """일곱 항목을 순서대로 실행한다. 한 항목의 실패나 예외가 나머지를 막지 않는다."""
    ctx: dict = {}
    results = []
    for number, check in enumerate(_CHECKS, start=1):
        try:
            status, observed = check(exchange, feed, sleep, clock, ctx)
        except _Skip as exc:
            status, observed = "SKIP", str(exc)
        except _Fail as exc:
            status, observed = "FAIL", str(exc)
        except Exception as exc:       # noqa: BLE001 — 어떤 예외도 보고서의 FAIL이 된다
            status, observed = "FAIL", f"{type(exc).__name__}: {exc}"
        results.append({"id": number, "title": TITLES[number], "status": status, "observed": observed})
        if number == 6:
            results[-1]["followup"] = _followups(ctx)
    return results


def _followups(ctx: dict) -> list[str]:
    """6번이 끝난 시점에 데모 계정에 주문이나 BTC가 남았을 수 있는 경우의 수동 조치."""
    out = []
    if ctx.get("stop_id"):
        out.append(f"손절 주문 {ctx['stop_id']}이 아직 열려 있을 수 있음: 데모 계정에서 직접 취소한다.")
    if ctx.get("buy_unknown"):
        out.append(f"매수 주문 {ctx.get('buy_id')}의 결과를 확인하지 못함: 데모 계정에 BTC가 남았을 수 있으니 "
                   "잔고와 주문 내역을 직접 확인하고 필요하면 직접 매도한다.")
    elif ctx.get("unsold", 0) > 0:
        out.append(f"BTC {ctx['unsold']:.10g}가 매도되지 않고 남았을 수 있음: 직접 확인하고 매도한다.")
    return out


# ---- 보고서 ------------------------------------------------------------------------
def _cell(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return " ".join(str(text).split()).replace("|", "\\|")


def _next_actions(results: list[dict]) -> list[str]:
    st = {r["id"]: r["status"] for r in results}
    out = []
    if st.get(1) != "PASS":
        out.append("1번 미통과: 멈추고 사용자에게 보고한다.")
    if st.get(3) == "FAIL":
        out.append("3번 실패(통과 종목이 기준 미만이거나 조회 실패): 멈추고 사용자에게 보고한다.")
    if st.get(4) != "PASS":
        out.append("4번 미통과: 원인을 해결하고 다시 실행한다."
                   + (" (5, 6번은 건너뜀)" if st.get(5) == "SKIP" else ""))
    for r in results:
        out.extend(r.get("followup", []))
    if st.get(5) == "FAIL":
        out.append("5번 실패: `PROTECTIVE_STOPS_ENABLED = False`로 바꾸고 진행한다.")
    if st.get(6) == "FAIL":
        out.append("6번 실패: 데모 계정에 BTC가 남았을 수 있으니 직접 확인한다.")
    if st.get(7) == "FAIL":
        out.append("7번 실패: Task 5를 \"REST 캔들 주기 조회 피드\"로 바꿔 진행하고 사용자에게 알린다.")
    out.append("4번의 수수료 자산과 최소 주문 금액을 이후 작업의 가정과 비교하고, 다르면 사용자에게 알린다.")
    if all(r["status"] == "PASS" for r in results):
        out.insert(0, "모든 항목 통과: 설정 변경 없이 진행한다.")
    return out


def render_report(results: list[dict], generated_at: datetime, settings: Settings | None = None) -> str:
    secrets = (settings.api_key, settings.api_secret) if settings is not None else ()
    utc = generated_at.astimezone(timezone.utc)
    lines = ["# 코인 스나이퍼 데모 모드 확인 결과", "",
             f"- 생성 시각: {utc:%Y-%m-%d %H:%M:%S} UTC / {utc.astimezone(_KST):%Y-%m-%d %H:%M:%S} KST"]
    if settings is not None:
        lines += [f"- 모드: {settings.mode}", f"- REST: {settings.rest_base}", f"- 스트림: {settings.stream_base}"]
    lines += ["", "## 요약", "", "| # | 항목 | 결과 | 관찰값 |", "|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {_cell(r['title'])} | {r['status']} | {_cell(r['observed'], secrets)} |")
    lines += ["", "## 다음 조치", ""]
    lines += [f"- {a}" for a in _next_actions(results)]
    return "\n".join(lines) + "\n"


# ---- 진입점 ------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m coin_sniper.probe", description="데모 모드 확인")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="결과 문서 경로")
    args = parser.parse_args(argv)

    env_loader.load_dotenv(str(Path(__file__).resolve().parent.parent / ".env"))
    try:
        settings = load_settings(os.environ)
        if settings.mode != "demo":
            raise SettingsError(f"데모 확인은 MODE=demo에서만 실행합니다 (현재 {settings.mode})")
        settings.require_keys()
    except SettingsError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    exchange = BinanceExchange(settings)
    feed = BinanceFeed(settings)
    results = run_probe(exchange, feed, time.sleep, lambda: datetime.now(timezone.utc))
    report = render_report(results, datetime.now(timezone.utc), settings)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    for r in results:
        print(f"[{r['status']}] {r['id']}. {r['title']}")
    print(f"결과 문서: {out}")
    return 0 if all(r["status"] == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
