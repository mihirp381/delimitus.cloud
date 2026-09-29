"""Secret scanning, run by the client before upload and again by the server (SSC-014).

A finding carries a masked value only. Files over 1 MiB, binary files, lockfiles and ``ssc.toml``
are skipped, and exact values of declared public build values are never flagged. A finding with
``blocking=False`` is a warning: a database URL whose password is a placeholder or whose host is
local (``localhost``, a compose service name).
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
BINARY_PROBE_BYTES: Final = 8192
MIN_GENERIC_LENGTH: Final = 24
MIN_GENERIC_ENTROPY: Final = 4.0
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
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.(eyJ[A-Za-z0-9_-]{8,})\.[A-Za-z0-9_-]{16,}")
_SB_SECRET = re.compile(r"sb_secret_[A-Za-z0-9_-]{16,}")
_SB_PUBLISHABLE = re.compile(r"sb_publishable_[A-Za-z0-9_-]{16,}")
_DB_URL = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|rediss?|amqps?)://"
    r"[^\s:/@\"'`<>]*:([^\s@/\"'`<>]+)@([^\s/:?#\"'`<>,;]+)"
)
_ASSIGNMENT = re.compile(
    r"(?i)[A-Za-z0-9_.-]*(?:secret|token|api_?key|passw(?:or)?d|private)[A-Za-z0-9_.-]*"
    r"[\"']?\s*(?::=|=>|=|:)\s*[\"'`]?([A-Za-z0-9+/=_.~-]{24,})"
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
    for m in _DB_URL.finditer(line):
        password, host = m.group(1), m.group(2).lower()
        hits.append(("db_url_with_password", password, not _harmless(password, host)))
    seen = {value for _, value, _ in hits}
    for m in _ASSIGNMENT.finditer(line):
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
