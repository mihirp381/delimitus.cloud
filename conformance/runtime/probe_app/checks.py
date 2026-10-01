"""The fourteen runtime probes as pure checks over what the probe app reports (SSC-017).

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
    "header_echo_no_google_jwt",
    "authorization_passthrough",
    "cannot_read_secrets",
    "no_platform_credentials_in_env",
    "sse_passthrough",
)
PROBES: Final = LOCAL_PROBES + CELL_PROBES
# Filesystems that keep data past the instance or leave it: none may be mounted in an app.
PERSISTENT_FS: Final = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "ceph", "glusterfs", "ext2", "ext3", "ext4", "xfs", "btrfs"}
)
GOOGLE_ISSUERS: Final = frozenset({"accounts.google.com", "https://accounts.google.com"})
REMOVED_SIGNATURE: Final = "SIGNATURE_REMOVED_BY_GOOGLE"
_JWT = re.compile(r"eyJ[\w-]+\.([\w-]+)\.([\w-]*)")
_CREDENTIAL_NAME = re.compile(
    r"^(GOOGLE_|CLOUDSDK_|GCLOUD_|AWS_|AZURE_|SSC_CELL_|SSC_GATEWAY|SSC_IMAGE)"
    r"|CREDENTIAL|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|API_KEY"
)
SSE_SPREAD_SECONDS: Final = 1.5


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
        if fs in PERSISTENT_FS or fs.startswith("fuse")
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
        raise ProbeFailedError(f"public names resolve: {leaked}")
    if _map(body.get("direct"), "direct").get("blocked") is not True:
        raise ProbeFailedError("a public resolver answers directly")
    return "public names do not resolve; no public resolver answers"


def metadata_token_no_roles(body: Body) -> str:
    if body.get("error"):
        raise ProbeFailedError(str(body["error"]))
    granted = body.get("granted")
    if granted != []:
        raise ProbeFailedError(f"granted {granted} (HTTP {body.get('granted_status')})")
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


def sse_passthrough(arrivals: list[float]) -> str:
    """``arrivals``: seconds from the request to each event. Buffering delivers them together."""
    if len(arrivals) != 3:  # noqa: PLR2004
        raise ProbeFailedError(f"{len(arrivals)} of 3 events arrived")
    spread = arrivals[-1] - arrivals[0]
    if spread < SSE_SPREAD_SECONDS:
        raise ProbeFailedError(f"events arrived {spread:.2f} s apart: buffered")
    return f"events streamed over {spread:.2f} s"
