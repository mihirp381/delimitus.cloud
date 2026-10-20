"""The routes an agent session must be refused, and one probe for each that changes nothing
when the refusal is missing (T4)."""

import json
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

import httpx2

from ssc_contracts.errors import CATALOGUE, ErrorCode

AGENT_SESSION_REFUSED: Final = "AGENT_SESSION_REFUSED"
ZERO: Final = "0" * 20
NO_CONNECTION: Final = "ssc-adversarial-probe"
NO_HOST: Final = "ssc-adversarial-probe.example.com"
HIGH_RISK_HOST: Final = "wetransfer.com"
NO_REPOSITORY: Final = "ssc-adversarial/never-installed"
RESERVED_SECRET: Final = "SSC_TOKEN"  # noqa: S105  (a secret name, not a value)
REASON: Final = "adversarial probe"
UNREACHABLE_COST: Final = 999_999
RATE_CALLS: Final = 200
MUTATING: Final = ("post", "put", "patch", "delete")
LEAKY: Final = re.compile(r"(HTTPS_PROXY|SSC_CONNECTION_[A-Z0-9_]+)\s*[=:]")
PASS: Final = "PASS"  # noqa: S105  (a result, not a password)
FAIL: Final = "FAIL"
SKIP: Final = "SKIP"

type Body = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Ids:
    app: str
    slug: str
    prod: str
    preview: str
    user: str
    approval: str = f"apr_{ZERO}"
    logs: bool = True


@dataclass(frozen=True, slots=True)
class Probe:
    method: str
    path: str
    body: Body | None = None
    expected: str = AGENT_SESSION_REFUSED


def _env(i: Ids, env: str) -> str:
    return f"/v1/apps/{i.app}/environments/{env}"


KNOWN: Final[Mapping[str, Callable[[Ids], Probe]]] = {
    "POST /v1/approvals/{approval_id}/decide": lambda i: Probe(
        "POST", f"/v1/approvals/{i.approval}/decide", {"outcome": "approved", "reason": REASON}
    ),
    # Operators only: a person's agent is FORBIDDEN before the agent check.
    "POST /v1/approvals/{approval_id}/decision": lambda i: Probe(
        "POST",
        f"/v1/approvals/{i.approval}/decision",
        {"outcome": "approved", "approver_user_id": i.user, "channel": "chat", "reason": REASON},
        "FORBIDDEN",
    ),
    "POST /v1/apps/{app_id}/delete": lambda i: Probe(
        "POST", f"/v1/apps/{i.app}/delete", {"confirm": f"not-{i.slug}"}
    ),
    "PUT /v1/apps/{app_id}/environments/{environment_id}/connections/{name}": lambda i: Probe(
        "PUT", f"{_env(i, i.preview)}/connections/{NO_CONNECTION}", {}
    ),
    "DELETE /v1/apps/{app_id}/environments/{environment_id}/connections/{name}": lambda i: Probe(
        "DELETE", f"{_env(i, i.preview)}/connections/{NO_CONNECTION}"
    ),
    "POST /v1/apps/{app_id}/environments/{environment_id}/database/rotate": lambda i: Probe(
        "POST", f"{_env(i, i.preview)}/database/rotate"
    ),
    "PUT /v1/apps/{app_id}/environments/{environment_id}/secrets/{name}": lambda i: Probe(
        "PUT", f"{_env(i, i.preview)}/secrets/{RESERVED_SECRET}", {"version": "1"}
    ),
    "POST /v1/apps/{app_id}/environments/{environment_id}/secrets/{name}/grants": lambda i: Probe(
        "POST", f"{_env(i, i.preview)}/secrets/{RESERVED_SECRET}/grants"
    ),
    "DELETE /v1/apps/{app_id}/github": lambda i: Probe("DELETE", f"/v1/apps/{i.app}/github"),
    "PUT /v1/apps/{app_id}/github": lambda i: Probe(
        "PUT", f"/v1/apps/{i.app}/github", {"repository": NO_REPOSITORY}
    ),
    "POST /v1/apps/{app_id}/promote": lambda i: Probe(
        "POST", f"/v1/apps/{i.app}/promote", {"preview_release_id": f"rel_{ZERO}"}
    ),
    "POST /v1/cell/resources/{resource}/enable": lambda _: Probe(
        "POST", "/v1/cell/resources/database/enable"
    ),
    "DELETE /v1/ci-tokens/{ci_token_id}": lambda _: Probe("DELETE", f"/v1/ci-tokens/ses_{ZERO}"),
    "POST /v1/connections": lambda _: Probe(
        "POST",
        "/v1/connections",
        {
            "name": NO_CONNECTION,
            "owner_user_id": f"usr_{ZERO}",
            "classification": "internal",
            "host": NO_HOST,
            "database": "none",
        },
    ),
    "PATCH /v1/connections/{name}": lambda _: Probe(
        "PATCH", f"/v1/connections/{NO_CONNECTION}", {"status": "suspended"}
    ),
    "DELETE /v1/egress/hosts/{host}": lambda _: Probe("DELETE", f"/v1/egress/hosts/{NO_HOST}"),
    "PUT /v1/egress/hosts/{host}": lambda _: Probe("PUT", f"/v1/egress/hosts/{HIGH_RISK_HOST}", {}),
    "PUT /v1/org/agent-policy": lambda i: Probe("PUT", "/v1/org/agent-policy", {"logs": i.logs}),
    "POST /v1/unlinked-logins/{unlinked_login_id}/link": lambda i: Probe(
        "POST", f"/v1/unlinked-logins/ulg_{ZERO}/link", {"user_id": i.user}
    ),
    "PUT /v1/warm": lambda i: Probe(
        "PUT",
        "/v1/warm",
        {"environment_ids": [i.prod], "gateway": False, "monthly_usd_shown": UNREACHABLE_COST},
    ),
}

AGENT_MAY: Final[Mapping[str, str]] = {
    "POST /v1/approvals": "asks; a person decides (decision 016)",
    "POST /v1/approvals/{approval_id}/cancel": "withdraws its own request (decision 016)",
    "POST /v1/apps": "create_app",
    "POST /v1/apps/{app_id}/bundles": "deploy",
    "POST /v1/apps/{app_id}/bundles/{bundle_id}/complete": "deploy",
    "POST /v1/apps/{app_id}/environments/{environment_id}/builds": "deploy",
    "POST /v1/apps/{app_id}/environments/{environment_id}/deployments": "deploy, rollback",
    "PUT /v1/apps/{app_id}/environments/{environment_id}/grants": "opens agent_share requests",
}

UNDECIDED: Final[Mapping[str, str]] = {
    "POST /v1/apps/{app_id}/kill-switch": "an agent session stops the app",
    "POST /v1/apps/{app_id}/enable": "an agent session starts a stopped app again",
    "PUT /v1/apps/{app_id}/owner": "an agent session gives the app to another member",
    "POST /v1/apps/{app_id}/environments/{environment_id}/schedules/{schedule_id}/pause": (
        "an agent session pauses a schedule"
    ),
    "POST /v1/apps/{app_id}/environments/{environment_id}/schedules/{schedule_id}/resume": (
        "an agent session resumes a schedule"
    ),
    "POST /v1/apps/{app_id}/environments/{environment_id}/schedules/{schedule_id}/runs": (
        "an agent session runs a job now"
    ),
}


def operations(spec: Mapping[str, Any]) -> Iterator[tuple[str, Mapping[str, Any]]]:
    paths = cast("Mapping[str, Mapping[str, Mapping[str, Any]]]", spec["paths"])
    for path, ops in paths.items():
        for method, op in ops.items():
            yield f"{method.upper()} {path}", op


def marked(spec: Mapping[str, Any]) -> set[str]:
    """The operations whose responses name ``AGENT_SESSION_REFUSED``."""
    return {
        key
        for key, op in operations(spec)
        if AGENT_SESSION_REFUSED in json.dumps(op.get("responses", {}))
    }


def unmarked_writes(spec: Mapping[str, Any]) -> set[str]:
    """The ``/v1`` writes an agent session is not refused."""
    return {
        key
        for key, _ in operations(spec)
        if key.split(" ")[0].lower() in MUTATING and key.split(" ")[1].startswith("/v1/")
    } - marked(spec)


def got_of(r: httpx2.Response) -> str:
    try:
        body: object = r.json()
    except ValueError:
        body = None
    code = cast("Body", body).get("code") if isinstance(body, dict) else None
    return f"{r.status_code} {code}" if code else str(r.status_code)


def problem(code: str) -> str:
    return f"{CATALOGUE[ErrorCode(code)].status} {code}"


def send(c: httpx2.Client, probe: Probe) -> httpx2.Response:
    headers = {"Idempotency-Key": uuid.uuid4().hex}
    if probe.body is None:
        return c.request(probe.method, probe.path, headers=headers)
    return c.request(probe.method, probe.path, json=probe.body, headers=headers)
