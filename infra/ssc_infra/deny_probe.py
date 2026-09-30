"""Done-when check 2: a service identity reading a secret value is denied.

    uv run python -m ssc_infra.deny_probe testcell01

Needs a cell stack with ``probe: true``. Both probe identities hold ``secretAccessor`` on
``ssc-a-probe``. The operator impersonates each: ``ssc-a-probe`` must read it (so the grant and
the impersonation work), then ``ssc-deny-probe`` must be refused, which only the deny rule can do.
The value goes to /dev/null; only exit codes and the error line are shown.
"""

import sys
import time
from dataclasses import dataclass
from typing import Final

from ssc_infra import naming as n
from ssc_infra.run import gcloud

PROPAGATION_SECONDS: Final = 600
RETRY_SECONDS: Final = 15


@dataclass(frozen=True, slots=True)
class Attempt:
    identity: str
    read: bool
    error: str


def attempt(label: str, account: str) -> Attempt:
    project = n.cell_project(label)
    identity = n.sa_email(account, project)
    result = gcloud(
        "secrets",
        "versions",
        "access",
        "latest",
        f"--secret={n.PROBE_SECRET}",
        f"--project={project}",
        f"--impersonate-service-account={identity}",
        "--verbosity=error",
        ok_codes=(0, 1),
        quiet=True,
    )
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    return Attempt(identity, result.returncode == 0, lines[-1] if lines else "")


def is_denial(error: str) -> bool:
    return "PERMISSION_DENIED" in error or "Permission" in error


def run(label: str, *, deadline: float, sleep: float = RETRY_SECONDS) -> tuple[Attempt, Attempt]:
    while True:
        allowed = attempt(label, n.PROBE_ALLOWED_SA)
        if allowed.read or time.monotonic() > deadline:
            break
        time.sleep(sleep)
    return allowed, attempt(label, n.PROBE_DENIED_SA)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)  # noqa: T201
        return 2
    allowed, denied = run(argv[0], deadline=time.monotonic() + PROPAGATION_SECONDS)
    print(f"{allowed.identity}: {'read' if allowed.read else 'refused: ' + allowed.error}")  # noqa: T201
    print(f"{denied.identity}: {'read' if denied.read else 'refused: ' + denied.error}")  # noqa: T201
    passed = allowed.read and not denied.read and is_denial(denied.error)
    print("PASS" if passed else "FAIL", file=sys.stderr)  # noqa: T201
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
