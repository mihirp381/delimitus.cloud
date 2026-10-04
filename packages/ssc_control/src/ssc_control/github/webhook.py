"""Webhook deliveries: the shared-secret signature and the one event that builds.

GitHub signs each delivery's body with the App's webhook secret as ``X-Hub-Signature-256:
sha256=<hex HMAC-SHA256>``; anything else, an unset secret included, is refused before the body
is parsed. Only ``push`` to a branch that was not deleted builds. A ``pull_request`` never does,
from a fork or not: a fork's code never reaches a build, and a branch in the repository builds
on its own push.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, Final, cast

SIGNATURE_HEADER: Final = "x-hub-signature-256"
EVENT_HEADER: Final = "x-github-event"
DELIVERY_HEADER: Final = "x-github-delivery"
MAX_BODY_BYTES: Final = 25 * 1024 * 1024
"""GitHub caps a delivery at 25 MB."""
BRANCH_PREFIX: Final = "refs/heads/"
_SIGNATURE: Final = re.compile(r"^sha256=([0-9a-f]{64})$")
_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_BRANCH: Final = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")


@dataclass(frozen=True, slots=True)
class Push:
    installation_id: int
    repository_id: int
    branch: str
    sha: str


def signature_ok(secret: bytes | None, body: bytes, header: str | None) -> bool:
    """True only for a body signed with ``secret``; constant-time."""
    if not secret or header is None:
        return False
    m = _SIGNATURE.fullmatch(header.strip())
    if m is None:
        return False
    want = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, m.group(1))


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _branch_and_sha(p: dict[str, Any]) -> tuple[str, str] | None:
    ref, sha = p.get("ref"), p.get("after")
    if p.get("deleted") is True or not isinstance(ref, str) or not isinstance(sha, str):
        return None
    branch = ref.removeprefix(BRANCH_PREFIX)
    if not ref.startswith(BRANCH_PREFIX) or not _BRANCH.fullmatch(branch):
        return None
    if not _SHA.fullmatch(sha) or sha == "0" * 40:
        return None
    return branch, sha


def _id_of(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    return _positive_int(cast("dict[str, Any]", value).get("id"))


def push_of(event: str | None, payload: object) -> Push | None:
    """The push to build, or None for every other delivery (``ping``, ``pull_request``, a tag,
    a deleted branch, an installation without an id)."""
    if event != "push" or not isinstance(payload, dict):
        return None
    p = cast("dict[str, Any]", payload)
    target = _branch_and_sha(p)
    installation_id, repository_id = _id_of(p.get("installation")), _id_of(p.get("repository"))
    if target is None or installation_id is None or repository_id is None:
        return None
    return Push(installation_id, repository_id, target[0], target[1])
