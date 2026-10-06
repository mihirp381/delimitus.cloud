"""The seventeen runtime probes as pure checks over what the probe app reports (SSC-017).

Each check takes the decoded body of one probe-app route (or what the runner measured) and
returns the reason it passed, or raises ``ProbeFailed``. Standard library only: the runner job
uses this module from inside the probe image.
"""

import base64
import ipaddress
import json
import re
from collections.abc import Mapping
from typing import Final, cast

EXPECTED_UID: Final = 10001
APP_CREDENTIAL: Final = "Bearer ssc-probe-app-credential"  # what the app's own client would send
LOCAL_PROBES: Final = (
    "non_root_10001",
    "listens_on_PORT",
    "health_path",
    "no_write_outside_memory",
)
CELL_PROBES: Final = (
    "no_direct_egress",
    "no_dns_exfil",
    "metadata_token_no_roles",
    "metadata_identity_is_own",
    "cannot_reach_peer_app",
    "cannot_reach_peer_cell",
    "deny_peer_cell",
    "header_echo_no_google_jwt",
    "authorization_passthrough",
    "cannot_read_secrets",
    "no_platform_credentials_in_env",
    "sse_passthrough",
    "datagw_read_only",
)
PROBES: Final = LOCAL_PROBES + CELL_PROBES
# Filesystems that keep data past the instance or leave it: none may be mounted in an app.
PERSISTENT_FS: Final = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "ceph", "glusterfs", "ext2", "ext3", "ext4", "xfs", "btrfs"}
)
# Cloud Run's own mount: files under /var/log go to the cell's Cloud Logging, as stdout does.
CLOUD_RUN_LOGS: Final = ("/var/log", "fuse.loggingfs")
GOOGLE_ISSUERS: Final = frozenset({"accounts.google.com", "https://accounts.google.com"})
REMOVED_SIGNATURE: Final = "SIGNATURE_REMOVED_BY_GOOGLE"
_JWT = re.compile(r"eyJ[\w-]+\.([\w-]+)\.([\w-]*)")
_CREDENTIAL_NAME = re.compile(
    r"^(GOOGLE_|CLOUDSDK_|GCLOUD_|AWS_|AZURE_|SSC_CELL_|SSC_GATEWAY|SSC_IMAGE)"
    r"|CREDENTIAL|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|API_KEY"
)
SSE_SPREAD_SECONDS: Final = 1.5
HTTP_OK: Final = 200
IAM_REFUSED: Final = 403
DENY_SECRET: Final = "ssc-a-probe"  # noqa: S105  (a secret's name, not a value)
DATAGW_REFUSED: Final = (422, "QUERY_REFUSED")
INGRESS_REFUSED: Final = 404
SAME_RANGE: Final = "range leg not applicable: same range, separate networks"
PROBE_APP_BODIES: Final = frozenset({'"ok"', "null"})
GATEWAY_PAGE: Final = "There is no app at this address"
GATEWAY_HEADERS: Final = {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


class ProbeFailedError(Exception):
    pass


type Body = Mapping[str, object]


def _map(value: object, what: str) -> Body:
    if not isinstance(value, Mapping):
        raise ProbeFailedError(f"unexpected {what}")
    return cast("Body", value)


def non_root(body: Body) -> str:
    uid = body.get("uid")
    if uid != EXPECTED_UID:
        raise ProbeFailedError(f"runs as uid {uid}, not {EXPECTED_UID}")
    return f"uid {uid}"


def no_write_outside_memory(body: Body) -> str:
    """In a cell the root filesystem is in memory (Cloud Run gen2), so writes there are allowed;
    what must not exist is a mount that keeps or ships data."""
    mounts = body.get("mounts")
    if not isinstance(mounts, list):
        raise ProbeFailedError("unexpected /probe/mounts body")
    persistent = [
        f"{point} ({fs})"
        for point, fs in cast("list[list[str]]", mounts)
        if (fs in PERSISTENT_FS or fs.startswith("fuse")) and (point, fs) != CLOUD_RUN_LOGS
    ]
    if persistent:
        raise ProbeFailedError(f"persistent mounts: {', '.join(persistent)}")
    if body.get("home_writable") is not True:
        raise ProbeFailedError(f"HOME ({body.get('home')}) is not writable")
    return f"{len(mounts)} mounts, none persistent; HOME={body.get('home')} is writable"


def no_direct_egress(body: Body) -> str:
    open_ = [k for k, v in body.items() if _map(v, k).get("blocked") is not True]
    if open_:
        raise ProbeFailedError(f"reached the internet: {', '.join(open_)}")
    return f"{len(body)} direct connections refused"


def _public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_global


def no_dns_exfil(body: Body) -> str:
    answers = _map(body.get("answers"), "/probe/dns answers")
    leaked = {
        name: addresses
        for name, addresses in answers.items()
        if any(_public(a) for a in cast("list[str]", addresses))
    }
    if leaked:
        raise ProbeFailedError(f"public names resolve: {leaked}; seen: {_dns_context(body)}")
    if _map(body.get("direct"), "direct").get("blocked") is not True:
        raise ProbeFailedError("a public resolver answers directly")
    return "public names do not resolve; no public resolver answers"


def _dns_context(body: Body) -> str:
    return f"resolvers {body.get('resolvers')}, Google APIs at {body.get('google_api')}"


def metadata_token_no_roles(body: Body) -> str:
    if body.get("error"):
        raise ProbeFailedError(str(body["error"]))
    granted = body.get("granted")
    if granted != []:
        raise ProbeFailedError(
            f"granted {granted} (HTTP {body.get('granted_status')}: {body.get('granted_reason')})"
        )
    return "the app's token holds none of the sensitive permissions"


def metadata_identity_is_own(body: Body) -> str:
    want = f"{body.get('k_service')}@{body.get('project')}.iam.gserviceaccount.com"
    if not body.get("k_service") or body.get("email") != want:
        raise ProbeFailedError(f"runs as {body.get('email')}, not {want}")
    return f"runs as its own account {want}"


def cannot_reach_peer_app(body: Body) -> str:
    attempts = _map(body.get("attempts"), "/probe/peer attempts")
    if not attempts:
        raise ProbeFailedError(str(body.get("error") or "no attempts"))
    answered = [
        f"{name}: HTTP {status}"
        for name, attempt in attempts.items()
        if isinstance(status := _map(attempt, name).get("status"), int) and status < 400  # noqa: PLR2004
    ]
    if answered:
        raise ProbeFailedError(f"the peer answered: {', '.join(answered)}")
    return "; ".join(f"{k}: {v}" for k, v in attempts.items())


def cannot_reach_peer_cell(body: Body) -> str:
    """Another cell's app and gateway refuse this app before IAM: no connection, or Cloud Run's
    ingress refusal (404). An IAM refusal (401, 403), or a 404 the peer app or gateway sent itself,
    means the network let the call through. When the peer's range holds this app's own address
    the cells are separate networks on the same range: the range leg is not applicable, not
    counted, and said so in the reason."""
    if body.get("error"):
        raise ProbeFailedError(str(body["error"]))
    peer_range, overlap = body.get("range"), body.get("own_in_range")
    if overlap not in {True, False}:
        raise ProbeFailedError(f"cannot tell whether {peer_range} holds this app's own address")
    attempts = {
        name: _map(attempt, name)
        for name, attempt in _map(body.get("attempts"), "/probe/peer-cell attempts").items()
        if not (overlap and name.startswith("tcp "))
    }
    if not attempts:
        raise ProbeFailedError("no attempts")
    reached = [r for name, attempt in attempts.items() if (r := _let_through(name, attempt))]
    if reached:
        raise ProbeFailedError(f"the peer cell let calls through: {', '.join(reached)}")
    legs = [f"{k}: {_outcome(v)}" for k, v in attempts.items()]
    return "; ".join([*legs, SAME_RANGE] if overlap else legs)


def deny_peer_cell(body: Body) -> str:
    """With its own identity this app asks another cell's Secret Manager for a secret and Cloud
    Storage for a bucket's objects. Google's IAM must refuse both (403). A 200 is a breach; no
    answer, a 404 or any other status proves nothing about IAM, so it fails too."""
    if body.get("error"):
        raise ProbeFailedError(str(body["error"]))
    legs = {leg: _map(body.get(leg), leg) for leg in ("secret", "bucket")}
    breached = [name for name, leg in legs.items() if leg.get("status") == HTTP_OK]
    if breached:
        raise ProbeFailedError(f"BREACH: the peer cell's {', '.join(breached)} answered 200")
    wrong = [
        f"{name}: {_answer_of(leg, 'reason')}"
        for name, leg in legs.items()
        if leg.get("status") != IAM_REFUSED
    ]
    if wrong:
        raise ProbeFailedError(f"IAM did not refuse: {', '.join(wrong)}")
    return "; ".join(f"{name}: {_answer_of(leg, 'reason')}" for name, leg in legs.items())


def _answer_of(answer: Body, detail: str) -> str:
    """The status and ``detail`` of one call, or why it got no answer."""
    if answer.get("status") is None:
        return f"no answer ({answer.get('error')})"
    return f"HTTP {answer.get('status')} {answer.get(detail) or ''}".strip()


def _let_through(name: str, attempt: Body) -> str | None:
    if "status" not in attempt:
        refused = attempt.get("blocked") is True or bool(attempt.get("error"))
        return None if refused else f"{name}: {dict(attempt)}"
    if attempt["status"] != INGRESS_REFUSED:
        return f"{name}: HTTP {attempt['status']}"
    marker = _peer_marker(attempt)
    return f"{name}: HTTP 404 from the peer itself ({marker})" if marker else None


def _peer_marker(attempt: Body) -> str | None:
    """What shows an answer came from the peer probe app or gateway, not from Google's ingress."""
    headers = {k: str(v) for k, v in _map(attempt.get("headers") or {}, "headers").items()}
    server = headers.get("server", "").lower()
    body = str(attempt.get("body") or "")
    found = (
        ("probe app server header", server.startswith("basehttp/")),
        ("Envoy server header", "envoy" in server),
        ("probe app JSON", headers.get("content-type", "").startswith("application/json")),
        ("probe app body", body.strip() in PROBE_APP_BODIES),
        ("gateway page headers", headers.items() >= GATEWAY_HEADERS.items()),
        ("gateway page", GATEWAY_PAGE in body),
    )
    return ", ".join(name for name, hit in found if hit) or None


def _outcome(attempt: Body) -> str:
    return f"HTTP {attempt['status']}" if "status" in attempt else str(dict(attempt))


def _claims(segment: str) -> dict[str, object]:
    try:
        decoded = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except ValueError:
        return {}
    return cast("dict[str, object]", decoded) if isinstance(decoded, dict) else {}


def header_echo_no_google_jwt(body: Body) -> str:
    """No header reaching the app carries a Google-signed token it could replay."""
    usable = [
        name
        for name, value in body.items()
        for payload, signature in _JWT.findall(str(value))
        if signature not in ("", REMOVED_SIGNATURE)
        and _claims(payload).get("iss") in GOOGLE_ISSUERS
    ]
    if usable:
        raise ProbeFailedError(f"signed Google tokens reach the app in: {', '.join(usable)}")
    return "every Google token that reaches the app has its signature removed"


def authorization_passthrough(body: Body) -> str:
    if body.get("authorization") != APP_CREDENTIAL:
        raise ProbeFailedError("the app's own Authorization header did not arrive unchanged")
    return "Authorization reaches the app unchanged beside X-Serverless-Authorization"


def cannot_read_secrets(body: Body) -> str:
    statuses = {k: body.get(k) for k in ("secret_access_status", "secret_list_status")}
    if any(s != 403 for s in statuses.values()):  # noqa: PLR2004
        raise ProbeFailedError(f"secret calls were not refused: {statuses}")
    return "reading and listing secrets are refused (403)"


def no_platform_credentials_in_env(body: Body) -> str:
    names = body.get("names")
    if not isinstance(names, list):
        raise ProbeFailedError("unexpected /probe/env body")
    found = [n for n in cast("list[str]", names) if _CREDENTIAL_NAME.search(n)]
    if found:
        raise ProbeFailedError(f"credential-like variables: {', '.join(found)}")
    return f"{len(names)} variables, none a credential"


def datagw_read_only(body: Body) -> str:
    """Every write-shaped statement sent to the cell's data gateway is refused by its classifier
    (422 ``QUERY_REFUSED``). An answer of any other kind, a refusal for another reason included,
    proves nothing about the read-only guarantee."""
    if body.get("error"):
        raise ProbeFailedError(str(body["error"]))
    attempts = {
        name: _map(attempt, name)
        for name, attempt in _map(body.get("attempts"), "/probe/datagw attempts").items()
    }
    if not attempts:
        raise ProbeFailedError("no statements were sent")
    wrong = [
        f"{name}: {_answer_of(attempt, 'code')}"
        for name, attempt in attempts.items()
        if (attempt.get("status"), attempt.get("code")) != DATAGW_REFUSED
    ]
    if wrong:
        raise ProbeFailedError(f"not refused as QUERY_REFUSED: {', '.join(wrong)}")
    return f"{len(attempts)} write-shaped statements refused (QUERY_REFUSED)"


def sse_passthrough(arrivals: list[float]) -> str:
    """``arrivals``: seconds from the request to each event. Buffering delivers them together."""
    if len(arrivals) != 3:  # noqa: PLR2004
        raise ProbeFailedError(f"{len(arrivals)} of 3 events arrived")
    spread = arrivals[-1] - arrivals[0]
    if spread < SSE_SPREAD_SECONDS:
        raise ProbeFailedError(f"events arrived {spread:.2f} s apart: buffered")
    return f"events streamed over {spread:.2f} s"
