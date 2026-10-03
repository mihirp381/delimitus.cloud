"""Done-when check for the public entry (SSC-088): the gateway answers only through its load
balancer.

    uv run python -m ssc_infra.entry_probe testcell01 [<project number>]

Asks ``https://www.<cell label>.<apps domain>/`` until the load balancer answers over a valid
certificate (up to 30 minutes, the certificate's done-when), then the gateway's own ``run.app``
host directly. ``www`` is a reserved slug, so no app ever answers there. The direct request must
be refused by ingress: a 403 or 404 that is not the answer the gateway gave through the load
balancer. Without a project number it is read from the cell stack's outputs.
"""

import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from typing import IO, Final, override

from ssc_infra import naming as n
from ssc_infra.run import CommandError, pulumi

PROBE_HOST: Final = "www"
REFUSED: Final = frozenset({403, 404})
CERTIFICATE_SECONDS: Final = 1800
RETRY_SECONDS: Final = 30
REQUEST_SECONDS: Final = 20
MAX_BODY: Final = 65536


@dataclass(frozen=True, slots=True)
class Answer:
    url: str
    status: int
    body: bytes
    error: str

    def describe(self) -> str:
        return f"{self.url}: {self.status or 'no answer'} {self.error}".rstrip()


type Fetch = Callable[[str], Answer]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer to compare, not one to follow."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def fetch(url: str) -> Answer:
    """One GET; any HTTP status is an answer, a TLS or connection failure is status 0."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=REQUEST_SECONDS) as response:
            return Answer(url, response.status, response.read(MAX_BODY), "")
    except urllib.error.HTTPError as exc:
        return Answer(url, exc.code, exc.read(MAX_BODY), "")
    except (urllib.error.URLError, OSError) as exc:
        return Answer(url, 0, b"", str(getattr(exc, "reason", exc)))


def public_url(label: str) -> str:
    return f"https://{PROBE_HOST}.{n.host_suffix(label)}/"


def direct_url(project_number: str) -> str:
    return n.run_url(n.GATEWAY, project_number) + "/"


def refused(direct: Answer, through: Answer) -> bool:
    """Ingress, not the gateway, answered the direct request."""
    return direct.status in REFUSED and (direct.status, direct.body) != (
        through.status,
        through.body,
    )


def run(
    label: str,
    project_number: str,
    *,
    get: Fetch,
    deadline: float,
    sleep: float,
) -> tuple[Answer, Answer]:
    """The answer through the load balancer once it has one (or at the deadline), then the
    direct answer."""
    while True:
        through = get(public_url(label))
        if through.status or time.monotonic() > deadline:
            break
        time.sleep(sleep)
    return through, get(direct_url(project_number))


def stack_project_number(label: str) -> str:
    return pulumi("stack", "output", "project_number", "--stack", n.cell_stack(label)).strip()


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print(__doc__, file=sys.stderr)  # noqa: T201
        return 2
    label = argv[0]
    try:
        number = argv[1] if len(argv) == 2 else stack_project_number(label)  # noqa: PLR2004
    except (CommandError, ValueError) as exc:
        print(exc, file=sys.stderr)  # noqa: T201
        return 1
    start = time.monotonic()
    through, direct = run(
        label, number, get=fetch, deadline=start + CERTIFICATE_SECONDS, sleep=RETRY_SECONDS
    )
    print(f"through the load balancer, after {time.monotonic() - start:.0f} s:", file=sys.stderr)  # noqa: T201
    print(f"  {through.describe()}", file=sys.stderr)  # noqa: T201
    print(f"direct: {direct.describe()}", file=sys.stderr)  # noqa: T201
    passed = bool(through.status) and refused(direct, through)
    print("PASS" if passed else "FAIL", file=sys.stderr)  # noqa: T201
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
