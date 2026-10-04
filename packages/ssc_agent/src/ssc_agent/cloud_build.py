"""Builds on the cell's Cloud Build, over the REST API v1 (SSC-015).

Runs as the cell agent, which may create, get and list builds and act as the cell's ``ssc-build``
account. Each build runs as ``ssc-build``, logs to Cloud Logging only, carries the SSC build id
as a tag (so ``start`` finds a build it already made) and has five steps, all in the platform's
tools image, pinned by digest:

1. ``fetch``: download the bundle from its signed URL, check its sha256, unpack it. ``ssc-build``
   holds no storage role: the URL is all it can read, and it can list no bucket.
2. ``scan``: gitleaks with ``--redact`` over the source, default rules plus SSC's (Supabase keys,
   database URLs with a password; ``gitleaks_config``). A finding stops the build.
3. ``plan``: ``railpack prepare`` with the public build values, the manifest's start command and
   the system packages from the platform package list the source needs (``system_packages``,
   SSC-093), installed in the build and in the image. A Dockerfile in the source is never used.
4. ``build``: BuildKit with the Railpack frontend, pinned by digest. On failure the log is
   classified (``PRIVATE_REGISTRY``, ``DEPENDENCY_UNRESOLVED``).
5. ``harden``: a platform-written layer on top: user 10001, ``HOME=/tmp``.

Cloud Build pushes the image through ``images:``, so the digest in ``results.images`` is the one
the registry holds. A step reports its reason through its exit code (``EXIT_CODES``); a step that
fails another way is ``BUILD_EXITED_NONZERO`` for the app's build and ``BUILD_DRIVER_ERROR`` for
the platform's steps. Build egress is open in the MVP (decision 014, SSC-015 amendment).

Cloud Build substitutes ``$NAME`` in a build's strings, so every ``$`` in a step is written as
``$$`` (``_literal``); no SSC value reaches a step except through its environment.
"""

import base64
import json
import re
import string
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast
from urllib.parse import quote

import httpx2

from ssc_contracts.build import (
    BUILD_DEPENDENCY_UNRESOLVED,
    BUILD_EXITED_NONZERO,
    BUILD_NO_ENTRYPOINT,
    BUILD_PRIVATE_REGISTRY,
    SECRET_IN_BUNDLE,
)
from ssc_shared.build import (
    BuildDriverError,
    BuildNotFoundError,
    BuildStatus,
    CellBuild,
    CellBuilder,
    Failed,
    Running,
    Succeeded,
)

type AccessTokens = Callable[[], Awaitable[str]]
type Json = dict[str, Any]

BUILD_API: Final = "https://cloudbuild.googleapis.com/v1"
BUILD_TAG: Final = "ssc-build"
BUILD_TIMEOUT: Final = "1200s"
QUEUE_TTL: Final = "300s"
"""Shorter than the bundle URL's 10 minutes, so a build never starts with an expired URL."""
APP_USER: Final = "10001:10001"
BUILD_DRIVER_ERROR: Final = "BUILD_DRIVER_ERROR"
BUILD_TIMED_OUT: Final = "BUILD_TIMED_OUT"
CALL_TIMEOUT_SECONDS: Final = 30.0
EXIT_CODES: Final[Mapping[int, str]] = {
    10: SECRET_IN_BUNDLE,
    11: BUILD_DEPENDENCY_UNRESOLVED,
    12: BUILD_PRIVATE_REGISTRY,
    13: BUILD_NO_ENTRYPOINT,
    14: BUILD_EXITED_NONZERO,
}
PRIVATE_REGISTRY: Final = (
    r"NameResolutionError|Failed to resolve|Could not resolve host"
    r"|getaddrinfo (ENOTFOUND|EAI_AGAIN)|npm (error|ERR!) code E40[13]"
    r"|401 Unauthorized|403 Forbidden|authentication required"
    r"|Permission denied \(publickey\)|could not read Username"
)
"""ERE, case-insensitive: the registry could not be reached or refused us."""
DEPENDENCY_UNRESOLVED: Final = (
    r"No matching distribution found|Could not find a version that satisfies"
    r"|npm (error|ERR!) code (E404|ETARGET)"
    r"|ERR_PNPM_(FETCH_404|NO_MATCHING_VERSION|OUTDATED_LOCKFILE)"
    r"|Couldn't find package|No solution found when resolving|ResolutionImpossible"
    r"|version solving failed|can only install packages when your package\.json"
    r"|lockfile is frozen|lockfile at .* needs to be updated"
    r"|changed significantly since poetry\.lock"
)
"""ERE, case-insensitive: a name or version that does not exist, or a stale lock file."""
STEP_IDS: Final = ("fetch", "scan", "plan", "build", "harden")
APP_STEPS: Final = frozenset({"build"})

_PINNED = re.compile(r"[a-z0-9.-]+(?::\d+)?(?:/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}")
_EMAIL = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9-]+\.iam\.gserviceaccount\.com")
_CLOUD_BUILD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EXIT_STATUS = re.compile(r"non-zero status:\s*(\d+)")
_HTTP_NOT_FOUND: Final = 404
_HTTP_BAD_REQUEST: Final = 400
_RUNNING: Final = frozenset({"STATUS_UNKNOWN", "PENDING", "QUEUED", "WORKING"})

FETCH: Final = """set -euo pipefail
mkdir -p /workspace/src /workspace/.ssc/secrets
curl --fail --silent --show-error --location --max-time 300 \\
  --output /workspace/.ssc/bundle.tar.gz "$SSC_BUNDLE_URL"
got="$(sha256sum < /workspace/.ssc/bundle.tar.gz | cut -d' ' -f1)"
if [ "$got" != "$SSC_BUNDLE_SHA256" ]; then echo "bundle sha256 mismatch" >&2; exit 1; fi
tar -xzf /workspace/.ssc/bundle.tar.gz -C /workspace/src --no-same-owner --no-same-permissions
rm /workspace/.ssc/bundle.tar.gz
"""
SCAN: Final = """set -uo pipefail
printf '%s' "$SSC_GITLEAKS_CONFIG" | base64 -d > /workspace/.ssc/gitleaks.toml
gitleaks dir /workspace/src --config /workspace/.ssc/gitleaks.toml --redact --verbose \\
  --no-banner --max-decode-depth 2 --exit-code 10
"""
PLAN: Final = """set -uo pipefail
args=(prepare /workspace/src --plan-out /workspace/.ssc/plan.json
      --info-out /workspace/.ssc/info.json --error-missing-start)
if [ -n "${SSC_START:-}" ]; then args+=(--start-cmd "$SSC_START"); fi
if [ -n "${SSC_APT_PACKAGES:-}" ]; then
  args+=(--env "RAILPACK_BUILD_APT_PACKAGES=$SSC_APT_PACKAGES")
  args+=(--env "RAILPACK_DEPLOY_APT_PACKAGES=$SSC_APT_PACKAGES")
fi
while IFS= read -r -d '' pair; do
  args+=(--env "$pair")
  printf '%s' "${pair#*=}" > "/workspace/.ssc/secrets/${pair%%=*}"
done < <(printf '%s' "$SSC_PUBLIC_ENV" | base64 -d)
railpack "${args[@]}" || exit 13
"""
BUILD: Final = """set -uo pipefail
args=(build --progress=plain --build-arg "BUILDKIT_SYNTAX=$SSC_FRONTEND"
      --file /workspace/.ssc/plan.json --tag ssc-app:built)
for f in /workspace/.ssc/secrets/*; do
  [ -e "$f" ] && args+=(--secret "id=$(basename "$f"),src=$f")
done
DOCKER_BUILDKIT=1 docker "${args[@]}" /workspace/src 2>&1 | tee /workspace/.ssc/build.log
[ "${PIPESTATUS[0]}" -eq 0 ] && exit 0
grep -Eqi "$SSC_PRIVATE_REGISTRY" /workspace/.ssc/build.log && exit 12
grep -Eqi "$SSC_DEPENDENCY_UNRESOLVED" /workspace/.ssc/build.log && exit 11
exit 14
"""
HARDEN: Final = """set -euo pipefail
mkdir -p /workspace/.ssc/harden
printf 'FROM ssc-app:built\\nUSER %s\\nENV HOME=/tmp\\n' "$SSC_APP_USER" \\
  > /workspace/.ssc/harden/Dockerfile
DOCKER_BUILDKIT=1 docker build --progress=plain --tag "$SSC_IMAGE" /workspace/.ssc/harden
"""
SCRIPTS: Final[Mapping[str, str]] = {
    "fetch": FETCH,
    "scan": SCAN,
    "plan": PLAN,
    "build": BUILD,
    "harden": HARDEN,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class CellBuildConfig:
    """Where and as whom builds run. Every name comes from the cell stack's outputs; both images
    are pinned by digest."""

    project: str
    region: str
    image_repository: str  # <region>-docker.pkg.dev/<p>/ssc-apps/apps
    service_account: str  # ssc-build@<p>.iam.gserviceaccount.com
    tools_image: str  # bash, curl, coreutils, tar, docker CLI, gitleaks 8.30, railpack 0.40
    frontend_image: str  # ghcr.io/railwayapp/railpack-frontend, the same Railpack version

    def __post_init__(self) -> None:
        for name in ("tools_image", "frontend_image"):
            if not _PINNED.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be pinned by digest (name@sha256:...)")
        if not _EMAIL.fullmatch(self.service_account):
            raise ValueError(f"not a service account email: {self.service_account!r}")

    @property
    def parent(self) -> str:
        return f"projects/{self.project}/locations/{self.region}"

    def image(self, build_id: str) -> str:
        return f"{self.image_repository}:{build_id}"


def gitleaks_config(public_values: Iterable[str]) -> str:
    """gitleaks' default rules (without ``jwt``: a Supabase anon key is public) plus SSC's
    Supabase and database URL rules, the database rule mirroring ``ssc_bundle.secrets``: a local
    host or a placeholder password is not a finding. Public build values never are."""
    allowed = sorted({v for v in public_values if v})
    lines = [
        'title = "ssc build"',
        "[extend]",
        "useDefault = true",
        'disabledRules = ["jwt"]',
        "",
        "[[rules]]",
        'id = "ssc-supabase-service-role"',
        'description = "Supabase service_role key"',
        "regex = '''\"role\"\\s*:\\s*\"service_role\"'''",
        "",
        "[[rules]]",
        'id = "ssc-supabase-secret-key"',
        'description = "Supabase secret key"',
        "regex = '''sb_secret_[A-Za-z0-9_-]{16,}'''",
        "",
        "[[rules]]",
        'id = "ssc-db-url-with-password"',
        'description = "Database URL with a password"',
        "regex = '''(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\\+srv)?|rediss?|amqps?)://"
        "[^\\s:/@\"'`<>]*:([^\\s@/\"'`<>]+)@([^\\s/:?#\"'`<>,;]+)'''",
        "secretGroup = 1",
        "[[rules.allowlists]]",
        'regexTarget = "match"',
        "regexes = [",
        "  '''@(?:localhost|127\\.0\\.0\\.1|0\\.0\\.0\\.0|\\[::1\\]|host\\.docker\\.internal)"
        "(?:[:/?#]|$)''',",
        "  ''':(?i:password|passwd|pass|pwd|secret|changeme|change_me|example|test|postgres"
        "|root|admin|dev|local|x+|\\*+|\\.+)@''',",
        "  ''':[$<{%]''',",
        "]",
    ]
    if allowed:
        lines += [
            "",
            "[[allowlists]]",
            'description = "public build values"',
            'regexTarget = "secret"',
            "regexes = [",
            *(f"  {json.dumps('^' + _re2_escape(v) + '$', ensure_ascii=False)}," for v in allowed),
            "]",
        ]
    return "\n".join(lines) + "\n"


def build_config(cell: CellBuildConfig, build: CellBuild) -> Json:
    """The Cloud Build ``Build`` resource for ``build``. Pure."""
    env_of: Mapping[str, Mapping[str, str]] = {
        "fetch": {"SSC_BUNDLE_URL": build.bundle_url, "SSC_BUNDLE_SHA256": build.bundle_sha256},
        "scan": {"SSC_GITLEAKS_CONFIG": _b64(gitleaks_config(build.public_env.values()))},
        "plan": {
            "SSC_START": build.start or "",
            "SSC_APT_PACKAGES": " ".join(build.system_packages),
            "SSC_PUBLIC_ENV": _b64("".join(f"{k}={v}\0" for k, v in build.public_env.items())),
        },
        "build": {
            "SSC_FRONTEND": cell.frontend_image,
            "SSC_PRIVATE_REGISTRY": PRIVATE_REGISTRY,
            "SSC_DEPENDENCY_UNRESOLVED": DEPENDENCY_UNRESOLVED,
        },
        "harden": {"SSC_APP_USER": APP_USER, "SSC_IMAGE": cell.image(build.build_id)},
    }
    steps = [
        {
            "id": step,
            "name": cell.tools_image,
            "entrypoint": "bash",
            "args": ["-c", _literal(SCRIPTS[step])],
            "env": [_literal(f"{k}={v}") for k, v in env_of[step].items()],
            **({"waitFor": [STEP_IDS[i - 1]]} if i else {"waitFor": ["-"]}),
        }
        for i, step in enumerate(STEP_IDS)
    ]
    return {
        "steps": steps,
        "images": [cell.image(build.build_id)],
        "serviceAccount": f"projects/{cell.project}/serviceAccounts/{cell.service_account}",
        "options": {"logging": "CLOUD_LOGGING_ONLY"},
        "timeout": BUILD_TIMEOUT,
        "queueTtl": QUEUE_TTL,
        "tags": [BUILD_TAG, build.build_id],
    }


def status_of(build: Json, image: str | None = None) -> BuildStatus:
    """A Cloud Build ``Build`` as SSC's status. Pure."""
    status = str(build.get("status") or "STATUS_UNKNOWN")
    if status in _RUNNING:
        return Running()
    name = str(build.get("name") or build.get("id") or "")
    if status == "SUCCESS":
        images = _objs(_obj(build.get("results")).get("images"))
        pushed = [i for i in images if image is None or i.get("name") == image] or images
        digest = str(pushed[0].get("digest") or "") if pushed else ""
        try:
            return Succeeded(image_digest=digest, scan_refs=(f"gitleaks:{name}",))
        except ValueError:
            return Failed(code=BUILD_DRIVER_ERROR, message=f"{name}: no pushed image digest")
    if status == "TIMEOUT":
        return Failed(code=BUILD_TIMED_OUT, message=f"{name}: timed out")
    if status != "FAILURE":
        return Failed(code=BUILD_DRIVER_ERROR, message=f"{name}: {status}")
    detail = str(_obj(build.get("failureInfo")).get("detail") or build.get("statusDetail") or "")
    m = _EXIT_STATUS.search(detail)
    code = EXIT_CODES.get(int(m.group(1))) if m else None
    if code is None:
        failed = [
            str(s.get("id")) for s in _objs(build.get("steps")) if s.get("status") != "SUCCESS"
        ]
        app_step = bool(failed) and failed[0] in APP_STEPS
        code = BUILD_EXITED_NONZERO if app_step else BUILD_DRIVER_ERROR
    return Failed(code=code, message=f"{name}: {detail}"[:1000])


class CloudBuildDriver(CellBuilder):
    def __init__(
        self,
        cell: CellBuildConfig,
        tokens: AccessTokens,
        *,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self.cell = cell
        self._tokens = tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def start(self, build: CellBuild) -> str:
        url = f"{BUILD_API}/{self.cell.parent}/builds"
        found = await self._call("GET", url, params={"filter": f'tags="{build.build_id}"'})
        existing = [b for b in _objs(found.get("builds")) if BUILD_TAG in _strs(b.get("tags"))]
        if existing:
            return str(existing[0]["id"])
        operation = await self._call("POST", url, json=build_config(self.cell, build))
        made = _obj(_obj(operation.get("metadata")).get("build"))
        ref = made.get("id")
        if not isinstance(ref, str) or not _CLOUD_BUILD_ID.fullmatch(ref):
            raise BuildDriverError(f"{build.build_id}: Cloud Build returned no build id")
        return ref

    async def poll(self, ref: str) -> BuildStatus:
        if not _CLOUD_BUILD_ID.fullmatch(ref):
            raise BuildNotFoundError(ref)
        url = f"{BUILD_API}/{self.cell.parent}/builds/{quote(ref)}"
        try:
            build = await self._call("GET", url)
        except _ApiError as exc:
            if exc.status == _HTTP_NOT_FOUND:
                raise BuildNotFoundError(ref) from None
            raise
        tags = _strs(build.get("tags"))
        if BUILD_TAG not in tags:
            raise BuildNotFoundError(ref)
        build_ids = [t for t in tags if t.startswith("bld_")]
        return status_of(build, self.cell.image(build_ids[0]) if build_ids else None)

    async def _call(
        self,
        method: str,
        url: str,
        *,
        json: Json | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Json:
        what = f"{method} {url.removeprefix(BUILD_API)}"
        headers = {"Authorization": f"Bearer {await self._tokens()}"}
        try:
            response = await self._client.request(
                method, url, json=json, params=dict(params or {}), headers=headers
            )
        except httpx2.HTTPError as exc:
            raise BuildDriverError(f"{what}: {type(exc).__name__}") from None
        if response.status_code >= _HTTP_BAD_REQUEST:
            raise _ApiError(what, response.status_code, response.reason_phrase)
        if not response.content:
            return {}
        return cast(Json, response.json())


class _ApiError(BuildDriverError):
    def __init__(self, what: str, status: int, reason: str) -> None:
        super().__init__(f"{what}: HTTP {status} {reason}")
        self.status = status


def _literal(value: str) -> str:
    return value.replace("$", "$$")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _re2_escape(value: str) -> str:
    return "".join("\\" + c if c in string.punctuation else c for c in value)


def _obj(value: object) -> Json:
    return cast(Json, value) if isinstance(value, dict) else {}


def _objs(value: object) -> list[Json]:
    items = cast("list[object]", value) if isinstance(value, list) else []
    return [cast(Json, v) for v in items if isinstance(v, dict)]


def _strs(value: object) -> Sequence[str]:
    items = cast("list[object]", value) if isinstance(value, list) else []
    return [v for v in items if isinstance(v, str)]
