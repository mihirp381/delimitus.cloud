"""``gcloud`` and ``pulumi`` as subprocesses, with the live Delimitus project fenced off."""

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any, Final

from ssc_infra import naming as n

FORBIDDEN_PROJECT: Final = "ristretto-506621"


class CommandError(RuntimeError):
    pass


def _check(args: Sequence[str]) -> None:
    if any(FORBIDDEN_PROJECT in a for a in args):
        raise CommandError(f"refusing to touch {FORBIDDEN_PROJECT}")


def gcloud(
    *args: str, ok_codes: Sequence[int] = (0,), quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    """``gcloud`` with prompts off. ``quiet`` sends stdout nowhere (used where it could hold a
    secret value); stderr is always captured, never streamed."""
    cmd = ["gcloud", *args, "--quiet"]
    _check(cmd)
    result = subprocess.run(  # noqa: S603
        cmd,
        stdout=subprocess.DEVNULL if quiet else subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode not in ok_codes:
        raise CommandError(
            f"gcloud {args[0]} {args[1] if len(args) > 1 else ''} failed: "
            f"{result.stderr.strip().splitlines()[-1:] or ['']}"
        )
    return result


def gcloud_json(*args: str) -> Any:
    out = gcloud(*args, "--format=json").stdout
    return json.loads(out) if out.strip() else None


def pulumi_env() -> Mapping[str, str]:
    return {**os.environ, "PULUMI_BACKEND_URL": f"gs://{n.STATE_BUCKET}"}


def pulumi(*args: str, cwd: str | None = None) -> str:
    cmd = ["pulumi", *args, "--non-interactive"]
    _check(cmd)
    result = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, check=False, env=dict(pulumi_env()), cwd=cwd
    )
    if result.returncode:
        raise CommandError(f"pulumi {' '.join(args[:2])} failed: {result.stderr.strip()[-2000:]}")
    return result.stdout
