"""T10: the gateway on the first-generation environment at 0.5 vCPU, with T3's probes.

``t10 --project <cell 1> --project-number <n> --app <probe a> --peer-app <probe b>
--ws-app <api probe app>``

The stack has no setting for the gateway's generation or CPU, so the gateway is switched by the
documented override in README T10 (``gcloud run services update``; Cloud Run asks for
concurrency 1 below 1 vCPU) and put back by ``pulumi up``. This command:

1. reads the gateway's settings and stops if they are not first generation at 0.5 vCPU;
2. runs T3's public probe set through the gateway;
3. holds a WebSocket to the API probe app through the gateway for five ticks;
4. prints the gateway's cost per busy hour on both settings.

Pass: the gateway is gen1 at 0.5 vCPU, 14 of 14 probes pass and the WebSocket carries its ticks.
Apps stay on gen2 either way.
"""

import argparse
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from proofrun import cloudrun, cost, t3, t8
from proofrun.common import (
    GATEWAY,
    CookieJar,
    Outcome,
    Run,
    app_environment,
    host_of,
    run_command,
    session_headers,
)
from proofrun.probes import probe_counts

GENERATION: Final = "gen1"
CPU: Final = 0.5
TICKS: Final = 5


def add_arguments(parser: argparse.ArgumentParser) -> None:
    t3.add_public_arguments(parser)
    parser.add_argument("--ws-app", required=True, help="the API probe app's slug")


def costs() -> list[str]:
    """The gateway's dollars per busy hour on each setting, request-billed, 512 MiB."""
    now = cost.hourly("request", 1.0, 0.5)
    low = cost.hourly("request", CPU, 0.5)
    return [
        f"gateway per busy hour: gen2 1 vCPU ${now:.4f}, gen1 0.5 vCPU ${low:.4f} "
        f"({(1 - low / now) * 100:.0f} % less)",
        "at concurrency 1 every open session holds its own gateway instance: multiply by the "
        "concurrent sessions, where at 1 vCPU and concurrency 1000 one instance carries them all",
    ]


def ws_ticks(
    socket: Callable[[str, str, Mapping[str, str]], Any], url: str, headers: Mapping[str, str]
) -> tuple[int, str]:
    """How many ticks arrived of ``TICKS``, and why it stopped short."""
    host = host_of(url)
    try:
        ws = socket(url, f"https://{host}", headers)
    except Exception as exc:  # noqa: BLE001  (a refused upgrade is a result)
        return 0, f"{type(exc).__name__}: {exc}"[:200]
    got = 0
    try:
        while got < TICKS:
            ws.recv(timeout=10.0)
            got += 1
    except Exception as exc:  # noqa: BLE001
        return got, f"{type(exc).__name__}: {exc}"[:200]
    finally:
        ws.close()
    return got, ""


def verdict(
    gateway: cloudrun.ServiceSettings,
    results: Sequence[Mapping[str, str]],
    ticks: int,
    why: str,
) -> Outcome:
    passed, total, bad = probe_counts(results)
    on_gen1 = gateway.generation == GENERATION and cloudrun.cpu_value(gateway.cpu) == CPU
    lines = [f"gateway: {gateway.describe()}"]
    lines += [f"{r['probe']}: {r['status']}: {r['reason']}" for r in results]
    lines.append(f"WebSocket: {ticks}/{TICKS} ticks" + (f", stopped: {why}" if why else ""))
    lines += costs()
    return Outcome(
        "T10",
        f"gen1 0.5 vCPU: {passed}/{total} probes, WebSocket {ticks}/{TICKS} ticks",
        on_gen1 and passed == total == 14 and not bad and ticks == TICKS,
        lines,
        {"passed": passed, "total": total, "ticks": ticks, "gateway": gateway.describe()},
    )


def run(
    args: argparse.Namespace,
    run: Run = run_command,
    socket: Callable[[str, str, Mapping[str, str]], Any] = t8.open_socket,
) -> Outcome:
    gateway = cloudrun.settings(cloudrun.describe(run, args.project, GATEWAY))
    if gateway.generation != GENERATION or cloudrun.cpu_value(gateway.cpu) != CPU:
        return Outcome(
            "T10",
            "the gateway is not on gen1 at 0.5 vCPU",
            None,
            [f"gateway: {gateway.describe()}", "apply the override in README T10 first"],
        )
    results = t3.public_results(args, run)
    env = app_environment(run, args.ws_app, args.env)
    host = host_of(env["url"])
    ticks, why = ws_ticks(socket, f"wss://{host}/ws", session_headers(CookieJar().get(host)))
    return verdict(gateway, results, ticks, why)
