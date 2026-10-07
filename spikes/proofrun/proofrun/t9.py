"""T9: a Streamlit app held open 24 hours, once instance-billed and once request-billed, then the
bill read against the model.

- ``t9 hold --state results/t9-instance.state.json --app <streamlit app> --project <cell 1>
  --mode instance`` checks the service is billed the way ``--mode`` says (``cpu-throttling``
  false for instance, true for request; README T9 for the request-billed override), then holds
  ``wss://<host>/_stcore/stream`` open, as a browser tab does, for ``--hours`` (24). When the
  stream drops it records when and why and reconnects at once, reading the session cookie
  again, so a cookie refreshed in the jar is picked up. A refused reconnect is recorded and
  retried every 30 s. Stopped, it resumes with the same command.
- ``t9 report --state <file>`` prints the hold: hours held, every drop with the length of the
  stream before it (the 60-minute drop is Cloud Run's request timeout), and the reconnects.
- ``t9 bill --state <file> --vcpu-seconds <n> --gib-seconds <n>`` (the usage amounts on the
  next day's bill for the app's service, so the free tier does not hide them), or
  ``--usd <amount>``. Pass: within 20 % of the hours held times $0.0909 (request) or $0.0684
  (instance) an hour.
"""

import argparse
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from proofrun import cloudrun, cost
from proofrun.common import (
    CookieJar,
    Outcome,
    Run,
    StateFile,
    app_environment,
    app_service,
    host_of,
    run_command,
    session_headers,
)

STREAM_PATH: Final = "/_stcore/stream"
SUBPROTOCOL: Final = "streamlit"
RETRY_S: Final = 30.0
HEARTBEAT_S: Final = 300.0
HOURS: Final = 24.0
EXPECTED_DROP_S: Final = 3600.0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="step", required=True)
    hold = sub.add_parser("hold", help="hold the stream open")
    hold.add_argument("--state", type=Path, required=True)
    hold.add_argument("--app", required=True, help="the Streamlit probe app's slug")
    hold.add_argument("--project", required=True, help="cell 1's project id")
    hold.add_argument("--mode", choices=sorted(cost.RATES), required=True)
    hold.add_argument("--hours", type=float, default=HOURS)
    hold.add_argument("--env", default="preview")
    report = sub.add_parser("report", help="what the hold saw")
    report.add_argument("--state", type=Path, required=True)
    bill = sub.add_parser("bill", help="the bill against the model")
    bill.add_argument("--state", type=Path, required=True)
    bill.add_argument("--vcpu-seconds", type=float)
    bill.add_argument("--gib-seconds", type=float)
    bill.add_argument("--usd", type=float, help="the service's cost before credits")


def held_seconds(state: Mapping[str, Any]) -> float:
    return sum(i["close"] - i["open"] for i in state.get("intervals", []))


def window_seconds(state: Mapping[str, Any]) -> float:
    """From the first stream opened to the last one closed: the hours the instance lived."""
    intervals = state.get("intervals", [])
    if not intervals:
        return 0.0
    return intervals[-1]["close"] - intervals[0]["open"]


def report(state: Mapping[str, Any]) -> Outcome:
    intervals: list[dict[str, Any]] = state.get("intervals", [])
    hours = float(state["config"]["hours"])
    held = held_seconds(state)
    window = window_seconds(state)
    lines = [
        f"stream {n + 1}: {(i['close'] - i['open']) / 60:.1f} min, ended {i.get('code')} "
        f"{i.get('reason') or ''}".rstrip()
        for n, i in enumerate(intervals)
    ]
    gaps = [b["open"] - a["close"] for a, b in zip(intervals, intervals[1:], strict=False)]
    lines.append(
        f"reconnects: {len(gaps)}, longest gap {max(gaps, default=0.0):.1f} s, "
        f"refused reconnects: {len(state.get('refusals', []))}"
    )
    lines += [f"refused at {r['at']:.0f}: {r['error']}" for r in state.get("refusals", [])]
    hour_drops = [
        i for i in intervals[:-1] if abs((i["close"] - i["open"]) - EXPECTED_DROP_S) <= 120
    ]
    lines.append(f"drops at about 60 minutes: {len(hour_drops)} of {max(len(intervals) - 1, 0)}")
    done = window >= hours * 3600 * 0.99
    return Outcome(
        f"T9 {state['config']['mode']}",
        f"held {held / 3600:.2f} h over {window / 3600:.2f} h, {len(gaps)} reconnects",
        (held >= 0.95 * window) if done else None,
        lines,
        {"held_s": held, "window_s": window, "reconnects": len(gaps)},
    )


def bill(
    state: Mapping[str, Any], *, usd: float | None, vcpu_s: float | None, gib_s: float | None
) -> Outcome:
    mode = state["config"]["mode"]
    hours = window_seconds(state) / 3600
    model = hours * cost.hourly(mode)
    if usd is None:
        if vcpu_s is None or gib_s is None:
            raise SystemExit("--usd, or both --vcpu-seconds and --gib-seconds")
        usd = cost.usage_cost(mode, vcpu_s, gib_s)
    lines = [
        f"hours held: {hours:.2f} at ${cost.hourly(mode):.4f} an hour ({mode}-billed, 1 vCPU, "
        f"512 MiB): model ${model:.4f}",
        f"bill: ${usd:.4f}"
        + (f" from {vcpu_s:.0f} vCPU-s and {gib_s:.0f} GiB-s" if vcpu_s is not None else ""),
    ]
    if vcpu_s is not None:
        lines.append(f"vCPU-seconds per hour held: {vcpu_s / hours:.0f} (3600 means always on)")
    off = (usd - model) / model * 100 if model else 0.0
    return Outcome(
        f"T9 {mode}",
        f"${usd:.4f} against ${model:.4f} ({off:+.1f} %)",
        cost.within(usd, model) if hours > 0 else None,
        lines,
        {"usd": usd, "model_usd": model, "hours": hours},
    )


def open_stream(url: str, origin: str, headers: Mapping[str, str]) -> Any:
    from websockets.sync.client import connect  # noqa: PLC0415
    from websockets.typing import Origin, Subprotocol  # noqa: PLC0415

    return connect(
        url,
        origin=Origin(origin),
        additional_headers=dict(headers),
        subprotocols=[Subprotocol(SUBPROTOCOL)],
        open_timeout=120,
    )


def hold(  # noqa: PLR0913  (keyword-only)
    store: StateFile,
    state: dict[str, Any],
    *,
    url: str,
    headers: Callable[[], Mapping[str, str]],
    socket: Callable[[str, str, Mapping[str, str]], Any] = open_stream,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Keep the stream open until the hold's hours are up, saving every change."""
    end = state.setdefault("ends_at", clock() + float(state["config"]["hours"]) * 3600)
    intervals: list[dict[str, Any]] = state.setdefault("intervals", [])
    refusals: list[dict[str, Any]] = state.setdefault("refusals", [])
    cut = state.pop("open", None)
    if cut is not None:
        intervals.append({**cut, "close": state.get("alive_at", cut["open"]), "code": "stopped"})
    store.save(state)
    origin = "https://" + host_of(url)
    while clock() < end:
        try:
            ws = socket(url, origin, headers())
        except Exception as exc:  # noqa: BLE001  (record any refusal, a missing cookie included)
            refusals.append({"at": clock(), "error": f"{type(exc).__name__}: {exc}"[:200]})
            store.save(state)
            say(f"reconnect refused ({type(exc).__name__}); retrying in {RETRY_S:.0f} s")
            sleep(RETRY_S)
            continue
        opened = clock()
        state["open"] = {"open": opened}
        state["alive_at"] = opened
        store.save(state)
        code, reason = _wait(ws, state, store, end, clock)
        intervals.append({"open": opened, "close": clock(), "code": code, "reason": reason})
        state.pop("open", None)
        store.save(state)
        say(f"stream ended after {(clock() - opened) / 60:.1f} min: {code} {reason or ''}")
    return state


def _wait(
    ws: Any, state: dict[str, Any], store: StateFile, end: float, clock: Callable[[], float]
) -> tuple[str, str]:
    beat = clock()
    try:
        while clock() < end:
            try:
                ws.recv(timeout=HEARTBEAT_S)
            except TimeoutError:
                pass
            if clock() - beat >= HEARTBEAT_S:
                beat = clock()
                state["alive_at"] = beat
                store.save(state)
        ws.close()
        return "held", "hours up"
    except Exception as exc:  # noqa: BLE001  (any end of the stream is a drop)
        code = getattr(getattr(exc, "rcvd", None), "code", None)
        return (str(code) if code is not None else type(exc).__name__), str(exc)[:200]


def run(args: argparse.Namespace, run: Run = run_command) -> Outcome:
    store = StateFile(args.state)
    if args.step != "hold":
        state = store.load()
        if state is None:
            return Outcome("T9", f"no state at {args.state}", None, [])
        if args.step == "report":
            return report(state)
        return bill(state, usd=args.usd, vcpu_s=args.vcpu_seconds, gib_s=args.gib_seconds)
    env = app_environment(run, args.app, args.env)
    service = cloudrun.settings(cloudrun.describe(run, args.project, app_service(env["id"])))
    throttled = service.cpu_throttled is not False
    if throttled != (args.mode == "request"):
        return Outcome(
            f"T9 {args.mode}",
            "the service is not billed that way",
            None,
            [f"service: {service.describe()}", "README T9 says how to switch it"],
        )
    config = {"app": args.app, "env": args.env, "mode": args.mode, "hours": args.hours}
    state = store.resume(config)
    host = host_of(env["url"])
    url = f"wss://{host}{STREAM_PATH}"
    state = hold(store, state, url=url, headers=lambda: session_headers(CookieJar().get(host)))
    return report(state)
