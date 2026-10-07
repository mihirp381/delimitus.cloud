"""SSC-090: hold ``/ws`` on a reconnect app past the 60-minute mark and check the numbers run on.

Run from ``spikes/proofrun``: ``uv run python apps/reconnect/check.py <host> --minutes 70``. On a
1012 close it reconnects at once with ``?after=<last>``, as ``sscSocket`` does; on any other end
it waits a second and tries again. It prints each connection and, at the end, PASS when it held
past 60 minutes with at least one 1012 reconnect and no gap or repeat in the numbers.
"""

import argparse
import time

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
from websockets.typing import Origin

from proofrun.common import CookieJar, session_headers

RESTART_CODE = 1012


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("--minutes", type=float, default=70.0)
    args = parser.parse_args()
    start = time.time()
    end = start + args.minutes * 60
    last, restarts, other, broken = -1, 0, 0, 0
    while time.time() < end:
        opened = time.time()
        try:
            headers = session_headers(CookieJar().get(args.host))
            with connect(
                f"wss://{args.host}/ws?after={last}",
                origin=Origin(f"https://{args.host}"),
                additional_headers=headers,
                open_timeout=120,
            ) as ws:
                while time.time() < end:
                    n = int(ws.recv(timeout=30))
                    if n != last + 1:
                        broken += 1
                        print(f"gap: {last} then {n}", flush=True)
                    last = n
                code = "held"
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else "no close frame"
        except Exception as exc:  # noqa: BLE001  (any other end is reported and retried)
            code = f"{type(exc).__name__}: {exc}"[:120]
        print(f"{time.strftime('%H:%M:%S')} connection held {(time.time() - opened) / 60:.1f} min,"
              f" last {last}, ended {code}", flush=True)
        if code == RESTART_CODE:
            restarts += 1
        elif code != "held":
            other += 1
            time.sleep(1)
    held = (time.time() - start) / 60
    ok = held >= 60 and restarts >= 1 and broken == 0
    print(f"{'PASS' if ok else 'FAIL'}: {held:.1f} min, {restarts} restart(s) on 1012,"
          f" {other} other end(s), {broken} gap(s), last number {last}")


if __name__ == "__main__":
    main()
