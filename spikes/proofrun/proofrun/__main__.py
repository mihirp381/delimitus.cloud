"""``uv run python -m proofrun <command> ...``: one command per proof, plus the kit's helpers.

- ``t1`` to ``t12``: the proofs (README for the order and the arguments).
- ``instances``: a service's Cloud Run metric, minute by minute (the deferred usage checks).
- ``cookie set <host>`` / ``cookie list``: keep a session cookie copied from a signed-in browser.
- ``stage-probe <dir>``: lay out the runtime probe app for ``ssc deploy``.
"""

import argparse
import getpass
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from proofrun import (
    drain,
    files,
    instances,
    rollback,
    secret_proof,
    sessions,
    t1,
    t2,
    t3,
    t4,
    t5,
    t6,
    t7,
    t8,
    t9,
    t10,
    t11,
    t12,
    timers,
    warm,
)
from proofrun.common import (
    KIT,
    CommandError,
    CookieError,
    CookieJar,
    Outcome,
    StateMismatchError,
    cookie_host,
    emit,
    fence,
    fence_environ,
)
from proofrun.probes import PROBE_APP

PROOFS: dict[str, ModuleType] = {
    "t1": t1,
    "t2": t2,
    "t3": t3,
    "t4": t4,
    "t5": t5,
    "t6": t6,
    "t7": t7,
    "t8": t8,
    "t9": t9,
    "t10": t10,
    "t11": t11,
    "t12": t12,
    "timers": timers,
    "files": files,
    "rollback": rollback,
    "secrets": secret_proof,
    "sessions": sessions,
    "warm": warm,
    "drain": drain,
    "instances": instances,
}


def stage_probe(target: Path) -> list[str]:
    """Copy the runtime probe app (``app.py`` alone: the runner and its checks stay out of the
    bundle) and the kit's manifest for it into ``target``."""
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for source in (PROBE_APP / "app.py", *sorted((KIT / "apps" / "probe").iterdir())):
        shutil.copyfile(source, target / source.name)
        copied.append(source.name)
    return copied


def cookie(args: argparse.Namespace, prompt: Callable[[str], str] = getpass.getpass) -> int:
    jar = CookieJar()
    if args.action == "list":
        for host, source, saved in jar.listing():
            print(f"{host}  {source}  saved {saved}")
        return 0
    value = prompt(f"value of the session cookie for {args.host} (not echoed): ").strip()
    jar.put(args.host, value, "browser")
    print(f"saved the cookie for {cookie_host(args.host)}")
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="python -m proofrun", description=(__doc__ or "").splitlines()[0]
    )
    sub = top.add_subparsers(dest="command", required=True)
    for name, module in PROOFS.items():
        module.add_arguments(sub.add_parser(name, help=(module.__doc__ or "").splitlines()[0]))
    jar = sub.add_parser("cookie", help="the session cookie jar")
    actions = jar.add_subparsers(dest="action", required=True)
    actions.add_parser("set").add_argument("host")
    actions.add_parser("list")
    stage = sub.add_parser("stage-probe", help="the runtime probe app laid out for ssc deploy")
    stage.add_argument("target", type=Path)
    return top


def main(argv: list[str]) -> int:
    fence(*argv)
    fence_environ(os.environ)
    args = parser().parse_args(argv)
    try:
        if args.command == "cookie":
            return cookie(args)
        if args.command == "stage-probe":
            print(f"staged {', '.join(stage_probe(args.target))} in {args.target}")
            return 0
        outcome: Outcome = PROOFS[args.command].run(args)
    except (CommandError, CookieError, StateMismatchError) as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 2
    return emit(outcome)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
