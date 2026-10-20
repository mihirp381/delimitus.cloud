"""Secret scanning, run by the client before upload and again by the server (SSC-014).

A finding carries a masked value only. Files over 1 MiB, binary files, lockfiles and ``ssc.toml``
are skipped, and exact values of declared public build values are never flagged. A finding with
``blocking=False`` is a warning: a database URL whose password is a placeholder or whose host is
local (``localhost``, a compose service name).

Every rule runs in time linear in the line (B-1): each run of a character class is bounded or
cannot overlap the next element, and a rule that can start inside a run is anchored to the run's
start, so one long line cannot hold the process. The name-keyed assignment rule reads only the
first ``MAX_LINE_SCAN`` (4096) characters of a line: a longer line is a minified asset or data,
not a config assignment. The rules for self-identifying values (AWS keys, private key blocks,
JWTs, Supabase keys, database URLs and the provider keys) read the whole line, so a key inside a
minified bundle is still found.

The provider rules (``_PROVIDER``: GitHub, GitLab, Slack, Stripe live, Anthropic, OpenAI, Google
API, npm and SendGrid keys) match by the value's own shape, under any variable name, as the
build's gitleaks does (GA-12 F22); a placeholder (entropy under ``MIN_PROVIDER_ENTROPY``, such as
``ghp_xxxx…``) is not a finding. The build still runs gitleaks' wider rule set.
"""

import base64
import json
import math
import re
from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Final, cast

from ssc_contracts.manifest import MANIFEST_FILE, Manifest

MAX_SCAN_BYTES: Final = 1024 * 1024
MAX_LINE_SCAN: Final = 4096
"""Characters of a line the name-keyed assignment rule reads."""
BINARY_PROBE_BYTES: Final = 8192
MIN_GENERIC_LENGTH: Final = 24
MIN_GENERIC_ENTROPY: Final = 4.0
MIN_PROVIDER_ENTROPY: Final = 3.0
LOCKFILES: Final = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "pdm.lock",
        "Cargo.lock",
        "composer.lock",
        "Gemfile.lock",
        "go.sum",
    }
)

_AWS_KEY = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")
_AWS_SECRET = re.compile(
    r"(?i)aws_?secret_?access_?key[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]{1,32} ){0,8}PRIVATE KEY(?: BLOCK)?-----")
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,4096}+\.(eyJ[A-Za-z0-9_-]{8,4096}+)"
    r"\.[A-Za-z0-9_-]{16,4096}"
)
_SB_SECRET = re.compile(r"(?<![A-Za-z0-9_-])sb_secret_[A-Za-z0-9_-]{16,4096}")
_SB_PUBLISHABLE = re.compile(r"sb_publishable_[A-Za-z0-9_-]{16,}")
_DB_URL = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|rediss?|amqps?)://"
    r"[^\s:/@\"'`<>]*:([^\s@/\"'`<>]+)@([^\s/:?#\"'`<>,;]+)"
)
_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]{0,64}(?:secret|token|api_?key|passw(?:or)?d|private)"
    r"[A-Za-z0-9_.-]{0,64}[\"']?\s*(?::=|=>|=|:)\s*[\"'`]?([A-Za-z0-9+/=_.~-]{24,})"
)
# Each starts with a literal and has only bounded runs; a rule that needs something after a
# variable-length run is anchored to the start of its run (B-1).
_PROVIDER: Final = (
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36}")),
    ("github_token", re.compile(r"github_pat_[A-Za-z0-9_]{82}")),
    ("gitlab_token", re.compile(r"glpat-[A-Za-z0-9_-]{20}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,100}")),
    ("stripe_live_key", re.compile(r"(?:sk|rk)_live_[A-Za-z0-9]{24,99}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{80,120}")),
    (
        "openai_key",
        re.compile(
            r"(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9_-]{20,200}T3BlbkFJ[A-Za-z0-9_-]{0,200}"
        ),
    ),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("npm_token", re.compile(r"npm_[A-Za-z0-9]{36}")),
    ("sendgrid_key", re.compile(r"SG\.[\w-]{22}\.[\w-]{43}")),
)
_PLACEHOLDER = re.compile(
    r"(?i)^(?:password|passwd|pass|pwd|secret|changeme|change_me|example|test|postgres|root"
    r"|admin|dev|local|x+|\*+|\.+)$|^[$<{%]"
)
_LOCAL_HOSTS: Final = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "host.docker.internal"}  # noqa: S104
)
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


@dataclass(frozen=True, slots=True, order=True)
class Finding:
    path: str
    line: int
    rule: str
    masked: str
    blocking: bool = True


def mask(value: str) -> str:
    """At most the first 4 and last 2 characters, fewer for short values."""
    head, tail = min(4, len(value) // 4), min(2, len(value) // 6)
    return value[:head] + "…" + (value[-tail:] if tail else "")


def allowed_values(manifest: Manifest) -> frozenset[str]:
    """Every public build value the manifest declares, in any environment."""
    return frozenset(v for values in manifest.build.public_env.values() for v in values.values())


def scan(files: Iterable[tuple[str, bytes]], allowed: Collection[str] = ()) -> list[Finding]:
    """Findings in path and line order. ``files`` are ``(POSIX path, content)`` pairs."""
    found: list[Finding] = []
    for path, data in files:
        if _skipped(path, data):
            continue
        text = data.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            found.extend(_scan_line(path, number, line, allowed))
    return sorted(found)


def _skipped(path: str, data: bytes) -> bool:
    return (
        path == MANIFEST_FILE
        or path.rsplit("/", 1)[-1] in LOCKFILES
        or len(data) > MAX_SCAN_BYTES
        or b"\0" in data[:BINARY_PROBE_BYTES]
    )


def _scan_line(path: str, number: int, line: str, allowed: Collection[str]) -> list[Finding]:
    hits: list[tuple[str, str, bool]] = []
    for m in _AWS_KEY.finditer(line):
        if "EXAMPLE" not in m.group(0):
            hits.append(("aws_access_key", m.group(0), True))
    hits.extend(("aws_access_key", m.group(1), True) for m in _AWS_SECRET.finditer(line))
    hits.extend(("private_key_block", m.group(0), True) for m in _PRIVATE_KEY.finditer(line))
    for m in _JWT.finditer(line):
        if _jwt_role(m.group(1)) == "service_role":
            hits.append(("supabase_service_key", m.group(0), True))
    hits.extend(("supabase_service_key", m.group(0), True) for m in _SB_SECRET.finditer(line))
    for rule, pattern in _PROVIDER:
        for m in pattern.finditer(line):
            if entropy(m.group(0)) >= MIN_PROVIDER_ENTROPY:
                hits.append((rule, m.group(0), True))
    for m in _DB_URL.finditer(line):
        password, host = m.group(1), m.group(2).lower()
        hits.append(("db_url_with_password", password, not _harmless(password, host)))
    seen = {value for _, value, _ in hits}
    for m in _ASSIGNMENT.finditer(line, 0, MAX_LINE_SCAN):
        value = m.group(1)
        if value not in seen and _generic_secret(value):
            hits.append(("generic_secret_assignment", value, True))
    return [
        Finding(path, number, rule, mask(value), blocking)
        for rule, value, blocking in hits
        if value not in allowed
    ]


def _jwt_role(payload: str) -> str | None:
    try:
        claims: object = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    if not isinstance(claims, dict):
        return None
    role = cast("dict[str, object]", claims).get("role")
    return role if isinstance(role, str) else None


def _harmless(password: str, host: str) -> bool:
    local = host in _LOCAL_HOSTS or ("." not in host and not _IPV4.match(host))
    return local or bool(_PLACEHOLDER.search(password))


def _generic_secret(value: str) -> bool:
    jwt = _JWT.fullmatch(value)
    if jwt:
        return _jwt_role(jwt.group(1)) != "anon"
    if "." in value or _SB_PUBLISHABLE.fullmatch(value):
        return False
    return (
        any(c.isdigit() for c in value)
        and any(c.isalpha() for c in value)
        and entropy(value) >= MIN_GENERIC_ENTROPY
    )


def entropy(value: str) -> float:
    """Shannon entropy in bits per character."""
    n = len(value)
    return -sum(c / n * math.log2(c / n) for c in Counter(value).values())
