"""The agent adversarial sweep against a live API (T4): ``python -m ssc_conformance.adversarial
--api URL --token TOKEN_OR_FILE --org ORG [--victim-app SLUG] --report PATH``.

The token is an agent's ``/v1`` credential (``ssc login --agent``): the token itself, or a file
holding it or the login's JSON. Each probe is chosen so that a missing refusal still changes
nothing, or is skipped. The one approval request it opens it withdraws; when that fails, the
operator withdraws the request whose id it prints.
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import httpx2
import jwt

from ssc_contracts.errors import CATALOGUE, ErrorCode
from ssc_shared.redaction import redact

AGENT_SESSION_REFUSED: Final = "AGENT_SESSION_REFUSED"
ZERO: Final = "0" * 20
NO_CONNECTION: Final = "ssc-adversarial-probe"
NO_HOST: Final = "ssc-adversarial-probe.example.com"
NO_EMAIL: Final = "ssc-adversarial-probe@example.com"
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
    # The agent check comes before the lookup, so an app that does not exist changes nothing.
    "POST /v1/apps/{app_id}/enable": lambda _: Probe("POST", f"/v1/apps/app_{ZERO}/enable"),
    "POST /v1/apps/{app_id}/kill-switch": lambda _: Probe(
        "POST", f"/v1/apps/app_{ZERO}/kill-switch", {"mode": "disable"}
    ),
    "PUT /v1/apps/{app_id}/owner": lambda i: Probe(
        "PUT", f"/v1/apps/app_{ZERO}/owner", {"user_id": i.user}
    ),
    "POST /v1/apps/{app_id}/environments/{environment_id}/schedules/{schedule_id}/pause": (
        lambda i: Probe("POST", f"{_env(i, i.preview)}/schedules/sch_{ZERO}/pause")
    ),
    "POST /v1/apps/{app_id}/environments/{environment_id}/schedules/{schedule_id}/resume": (
        lambda i: Probe("POST", f"{_env(i, i.preview)}/schedules/sch_{ZERO}/resume")
    ),
    "POST /v1/apps/{app_id}/environments/{environment_id}/schedules/{schedule_id}/runs": (
        lambda i: Probe("POST", f"{_env(i, i.preview)}/schedules/sch_{ZERO}/runs")
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
    # The roster names everyone; the exact email lookup stays open to an admin's agent.
    "GET /v1/users": lambda _: Probe("GET", "/v1/users"),
    "POST /v1/users/invite": lambda _: Probe("POST", "/v1/users/invite", {"email": NO_EMAIL}),
    "POST /v1/users/{user_id}/deactivate": lambda _: Probe(
        "POST", f"/v1/users/usr_{ZERO}/deactivate"
    ),
    "POST /v1/users/{user_id}/reactivate": lambda _: Probe(
        "POST", f"/v1/users/usr_{ZERO}/reactivate"
    ),
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
    "POST /v1/apps/{app_id}/environments/{environment_id}/grants/requirements": "writes nothing",
    "POST /v1/apps/{app_id}/environments/{environment_id}/connections/{name}/request": (
        "request_connection; on preview the connection's rule may link it (decision 016)"
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


# ── the live sweep ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Row:
    group: str
    case: str
    expected: str
    got: str
    result: str


@dataclass(frozen=True, slots=True)
class Credential:
    access: str
    refresh: str | None


def read_credential(value: str) -> Credential:
    path = Path(value)
    text = path.read_text(encoding="utf-8").strip() if path.is_file() else value.strip()
    if text.startswith("{"):
        login = cast("Body", json.loads(text))
        return Credential(str(login["access_token"]), login.get("refresh_token"))
    return Credential(text, None)


def claims_of(token: str) -> Body:
    return jwt.decode(token, options={"verify_signature": False})


def judged(group: str, case: str, expected: str, got: str) -> Row:
    return Row(group, case, expected, got, PASS if got == expected else FAIL)


def discover(c: httpx2.Client, ref: str | None, user: str) -> Ids:
    apps = cast("list[Body]", c.get("/v1/apps").raise_for_status().json()["apps"])
    chosen = next((a for a in apps if ref in (None, a["slug"], a["id"])), None)
    if chosen is None:
        raise SystemExit("adversarial: no app visible to this credential; pass --app")
    app = cast("Body", c.get(f"/v1/apps/{chosen['id']}").raise_for_status().json())
    envs = {e["name"]: e["id"] for e in cast("list[Body]", app["environments"])}
    logs = bool(c.get("/v1/org/agent-policy").raise_for_status().json()["logs"])
    return Ids(app["id"], app["slug"], envs["prod"], envs["preview"], user, logs=logs)


def guard(c: httpx2.Client, key: str, ids: Ids) -> str | None:
    """Why a probe would change something if its refusal were missing, or None."""
    if key.endswith("/database/rotate"):
        db = c.get(f"{_env(ids, ids.preview)}/database")
        if db.is_success and db.json().get("present"):
            return "preview has a database"
    if key == "DELETE /v1/apps/{app_id}/github" and c.get(f"/v1/apps/{ids.app}/github").is_success:
        return "the app has a repository link"
    if key == "POST /v1/cell/resources/{resource}/enable":
        cell = c.get("/v1/cell")
        states = {r["resource"]: r["state"] for r in cell.json().get("resources", [])}
        if not cell.is_success or states.get("database") in (None, "off", "failed"):
            return "the cell's database is not on"
    return None


def refusals(c: httpx2.Client, spec: Mapping[str, Any], ids: Ids) -> list[Row]:
    out: list[Row] = []
    for key in sorted(marked(spec)):
        build = KNOWN.get(key)
        if build is None:
            out.append(Row("1", key, "in KNOWN", "unmapped", FAIL))
            continue
        if (why := guard(c, key, ids)) is not None:
            out.append(Row("1", key, problem(build(ids).expected), f"skipped: {why}", SKIP))
            continue
        probe = build(ids)
        out.append(judged("1", key, problem(probe.expected), got_of(send(c, probe))))
    for key in sorted(unmarked_writes(spec) - AGENT_MAY.keys()):
        out.append(Row("1", key, "refused or listed", "unlisted write", FAIL))
    return out


def victim(c: httpx2.Client, ref: str) -> list[Row]:
    seen = {a["slug"] for a in cast("list[Body]", c.get("/v1/apps").json()["apps"])}
    seen |= {a["id"] for a in cast("list[Body]", c.get("/v1/apps").json()["apps"])}
    out = [
        judged("2", "list_apps hides the victim", "absent", "listed" if ref in seen else "absent")
    ]
    for path in ("/v1/connections", "/v1/approvals"):
        text = c.get(path).text
        out.append(
            judged("2", f"{path} hides the victim", "absent", "named" if ref in text else "absent")
        )
    if ref.startswith("app_"):
        for path in (f"/v1/apps/{ref}", f"/v1/apps/{ref}/releases", f"/v1/apps/{ref}/github"):
            out.append(judged("2", f"GET {path}", problem("NOT_FOUND"), got_of(c.get(path))))
    return out


def initialize(api: str, token: str | None) -> httpx2.Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-11-25",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "ssc-adversarial", "version": "0"},
        },
    }
    return httpx2.post(f"{api}/mcp", json=body, headers=headers, timeout=30)


def mcp_refused(api: str, token: str | None) -> str:
    r = initialize(api, token)
    meta = f'resource_metadata="{api}/.well-known/oauth-protected-resource/mcp"'
    challenge = r.headers.get("WWW-Authenticate", "")
    return "401 resource_metadata" if r.status_code == 401 and meta in challenge else got_of(r)


def unsigned(token: str) -> str:
    return jwt.encode(claims_of(token), key="", algorithm="none")


def tampered(token: str) -> str:
    head, payload, signature = token.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    return f"{head}.{payload}.{flipped}"


def audiences(api: str, cred: Credential) -> list[Row]:
    def on_v1(token: str | None) -> str:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        return got_of(httpx2.get(f"{api}/v1/apps", headers=headers, timeout=30))

    want = problem("UNAUTHENTICATED")
    out = [
        judged(
            "4", "API agent token on /mcp", "401 resource_metadata", mcp_refused(api, cred.access)
        ),
        judged("4", "no token on /v1", want, on_v1(None)),
        judged("4", "tampered signature on /v1", want, on_v1(tampered(cred.access))),
        judged("4", "unsigned (alg none) on /v1", want, on_v1(unsigned(cred.access))),
        judged("4", "not a token on /v1", want, on_v1("not-a-token")),
    ]
    if cred.refresh is None:
        out.append(Row("4", "refresh token as bearer", want, "skipped: no refresh token", SKIP))
    else:
        out.append(judged("4", "refresh token as bearer on /v1", want, on_v1(cred.refresh)))
        got = mcp_refused(api, cred.refresh)
        out.append(judged("4", "refresh token as bearer on /mcp", "401 resource_metadata", got))
    out.append(Row("4", "token after /revoke", want, "skipped: offline only", SKIP))
    return out


def clean(text: str) -> str:
    return "secret-shaped text" if redact(text) != text or LEAKY.search(text) else "clean"


def secrets(c: httpx2.Client, ids: Ids) -> list[Row]:
    app = c.get(f"/v1/apps/{ids.app}")
    out = [judged("5", "get_app", "clean", clean(app.text))]
    for env in cast("list[Body]", app.json()["environments"]):
        if dep := env.get("current_deployment_id"):
            got = clean(c.get(f"/v1/operations/{dep}").text)
            out.append(judged("5", f"get_status {env['name']}", "clean", got))
    for env, env_id in (("prod", ids.prod), ("preview", ids.preview)):
        r = c.get(f"{_env(ids, env_id)}/logs", params={"source": "app", "since": 3600})
        if got_of(r) == problem("AGENT_LOGS_OFF"):
            out.append(Row("5", f"get_logs {env}", "clean", "skipped: logs off for agents", SKIP))
            continue
        lines = cast("list[Body]", r.json().get("lines", [])) if r.is_success else []
        got = (
            clean("\n".join(str(line.get("text", "")) for line in lines))
            if r.is_success
            else got_of(r)
        )
        out.append(judged("5", f"get_logs {env}", "clean", got))
    names = [
        n["name"]
        for n in cast("list[Body]", c.get("/v1/connections").json().get("connections", []))
    ]
    for name in names:
        r = c.get(f"{_env(ids, ids.prod)}/connections/{name}/schema")
        out.append(judged("5", f"describe_connection {name}", "clean", clean(r.text)))
    return out


def approvals(c: httpx2.Client, spec: Mapping[str, Any], ids: Ids) -> tuple[list[Row], str | None]:
    asked = c.post(
        "/v1/approvals",
        json={
            "environment_id": ids.preview,
            "kind": "connect_data_source",
            "subject_key": NO_CONNECTION,
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    if asked.status_code != 201:
        return [judged("6", "open own request", "201", got_of(asked))], None
    own = Ids(ids.app, ids.slug, ids.prod, ids.preview, ids.user, asked.json()["id"], ids.logs)
    out = [judged("6", "open own request", "201", "201")]
    named = {
        "approve own request (decide)": "POST /v1/approvals/{approval_id}/decide",
        "record a decision on own request": "POST /v1/approvals/{approval_id}/decision",
        "promote": "POST /v1/apps/{app_id}/promote",
        "warm": "PUT /v1/warm",
        "repo link": "PUT /v1/apps/{app_id}/github",
        "egress": "PUT /v1/egress/hosts/{host}",
        "create connection": "POST /v1/connections",
        "delete app": "POST /v1/apps/{app_id}/delete",
        "link login": "POST /v1/unlinked-logins/{unlinked_login_id}/link",
    }
    for case, key in named.items():
        if key not in marked(spec):
            out.append(Row("6", case, AGENT_SESSION_REFUSED, "not marked in the spec", FAIL))
            continue
        probe = KNOWN[key](own)
        out.append(judged("6", case, problem(probe.expected), got_of(send(c, probe))))
    withdrawn = c.post(
        f"/v1/approvals/{own.approval}/cancel",
        json={"reason": REASON, "channel": "cli"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    out.append(judged("6", "withdraw own request (decision 016)", "200", got_of(withdrawn)))
    out.append(
        Row("6", "approve another org's request", "404 NOT_FOUND", "skipped: offline only", SKIP)
    )
    return out, None if withdrawn.is_success else own.approval


def rate(api: str, cred: Credential, second: str | None) -> list[Row]:
    headers = {"Authorization": f"Bearer {cred.access}"}
    with httpx2.Client(base_url=api, headers=headers, timeout=30) as c:
        limited, calls = None, 0
        for calls in range(1, RATE_CALLS + 1):  # noqa: B007  (the count is reported)
            r = c.get("/v1/apps")
            if r.status_code == 429:
                limited = r
                break
        if limited is None:
            return [
                Row(
                    "7",
                    "list_apps until limited",
                    problem("RATE_LIMITED"),
                    f"none in {calls}",
                    FAIL,
                )
            ]
        retry = limited.headers.get("Retry-After")
        got = f"{got_of(limited)} Retry-After" if retry else got_of(limited)
        out = [
            judged("7", f"limited at call {calls}", f"{problem('RATE_LIMITED')} Retry-After", got)
        ]
        if second is not None:
            other = httpx2.get(
                f"{api}/v1/apps", headers={"Authorization": f"Bearer {second}"}, timeout=30
            )
            out.append(judged("7", "a second credential is not limited", "200", got_of(other)))
        time.sleep(float(retry or 1) + 0.5)
        out.append(judged("7", "recovers after Retry-After", "200", got_of(c.get("/v1/apps"))))
    return out


def sweep(
    api: str, cred: Credential, *, app: str | None, victim_app: str | None, second: str | None
) -> tuple[list[Row], str | None]:
    claims = claims_of(cred.access)
    if not claims.get("agent"):
        raise SystemExit("adversarial: the token is not an agent's (ssc login --agent)")
    headers = {"Authorization": f"Bearer {cred.access}"}
    with httpx2.Client(base_url=api, headers=headers, timeout=30) as c:
        spec = cast("Body", c.get("/openapi.json").raise_for_status().json())
        ids = discover(c, app, str(claims["sub"]))
        rows = refusals(c, spec, ids)
        if victim_app is not None:
            rows += victim(c, victim_app)
        rows += audiences(api, cred)
        rows += secrets(c, ids)
        asked, left = approvals(c, spec, ids)
        rows += asked
    rows += rate(api, cred, second)
    return rows, left


def markdown(rows: Sequence[Row], left: str | None = None) -> str:
    lines = ["| group | case | expected | got | result |", "|---|---|---|---|---|"]
    lines += [f"| {r.group} | `{r.case}` | {r.expected} | {r.got} | {r.result} |" for r in rows]
    failed = sum(r.result == FAIL for r in rows)
    lines += ["", f"{len(rows)} probes, {failed} failed."]
    if left is not None:
        lines.append(f"Withdraw approval request `{left}`: the sweep could not.")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--api", required=True)
    parser.add_argument("--token", required=True, help="the agent's token, or a file holding it")
    parser.add_argument("--org", required=True)
    parser.add_argument("--victim-app", help="another org's app slug or id")
    parser.add_argument("--app", help="the caller's own app (default: the first visible)")
    parser.add_argument("--second-token", help="another agent credential of the same person")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    api = args.api.rstrip("/")
    cred = read_credential(args.token)
    if claims_of(cred.access).get("org") != args.org:
        sys.stderr.write("adversarial: the token is not for --org\n")
        return 2
    second = None if args.second_token is None else read_credential(args.second_token).access
    rows, left = sweep(api, cred, app=args.app, victim_app=args.victim_app, second=second)
    for r in rows:
        sys.stdout.write(f"{r.group} | {r.case} | {r.expected} | {r.got} | {r.result}\n")
    text = "## Agent adversarial sweep\n\n" + markdown(rows, left)
    args.report.write_text(text, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as f:
            f.write(text)
    if left is not None:
        sys.stderr.write(f"adversarial: withdraw approval request {left}\n")
    return 1 if any(r.result == FAIL for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
