"""Every suffix of the cell's org, walked nightly (CD-7.2):
``python -m ssc_conformance.suffix_walk``.

Reads the org's latest snapshot from the cell bucket (``<project>-cell``), checked against the
pointer's digest. The current suffix is the snapshot's ``suffix``, else ``SSC_PROBE_BASE``; the
previous ones are its ``previous_suffixes`` still before their ``until``. For each active
environment's host label, a GET without a session answers 200 or a 302 to ``<auth_url>/login`` on
the current suffix, and a 307 with ``Cache-Control: no-store`` to the same label under the current
suffix on a previous one. Each suffix's certificate has ``MIN_DAYS`` days or more left. A customer
suffix (any but ``SSC_PROBE_BASE``) answers the CAA record ``CAA`` over DNS-over-HTTPS; the default
suffix has no CAA of its own and is not asked.

Configuration, all required: ``SSC_PROBE_PROJECT``, ``SSC_PROBE_ORG_ID``, ``SSC_PROBE_BASE`` and
``SSC_PROBE_AUTH_URL``. The bucket is read with ``SSC_ACCESS_TOKEN`` or else ``gcloud``. Adds the
proof ``suffixes`` to ``SSC_EVIDENCE_FILE``; its reason names every suffix walked. Exits 1 when
any check fails.
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import quote

import httpx2

from ssc_conformance import evidence
from ssc_conformance.matrix import SUFFIXES
from ssc_conformance.nightly import (
    DaysLeft,
    NightlyError,
    certificate_days_left,
    gcloud_access_tokens,
)
from ssc_contracts.snapshot import SnapshotDoc
from ssc_control.domains.probe import DOH_ENDPOINTS
from ssc_shared.access import SnapshotInvalidError
from ssc_shared.snapshot_feed import latest_key, parse_pointer

ENV: Final = {
    "project": "SSC_PROBE_PROJECT",
    "org_id": "SSC_PROBE_ORG_ID",
    "base": "SSC_PROBE_BASE",
    "auth_url": "SSC_PROBE_AUTH_URL",
}
MIN_DAYS: Final = 14.0
CAA: Final = '0 issue "pki.goog"'
CAA_TYPE: Final = 257
DOH: Final = DOH_ENDPOINTS["google"]
STORAGE_API: Final = "https://storage.googleapis.com/storage/v1"
TIMEOUT_SECONDS: Final = 15.0


class WalkError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    headers: Mapping[str, str]
    body: bytes = b""


type Fetch = Callable[[str, Mapping[str, str]], Awaitable[Reply]]


def http_fetch(client: httpx2.AsyncClient) -> Fetch:
    """One GET that never follows a redirect; header names lower-cased."""

    async def fetch(url: str, headers: Mapping[str, str]) -> Reply:
        try:
            r = await client.get(url, headers=dict(headers), follow_redirects=False)
        except httpx2.HTTPError as exc:
            raise WalkError(f"{url.split('?', 1)[0]}: {type(exc).__name__}") from None
        return Reply(r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content)

    return fetch


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    project: str
    org_id: str
    base: str
    auth_url: str

    @property
    def bucket(self) -> str:
        return f"{self.project}-cell"


def config_from_env(environ: Mapping[str, str]) -> Config:
    values = {key: environ.get(name, "").strip() for key, name in ENV.items()}
    if missing := [ENV[k] for k, v in values.items() if not v]:
        raise WalkError(f"set {', '.join(missing)}")
    values["auth_url"] = values["auth_url"].rstrip("/")
    return Config(**values)


async def _object(fetch: Fetch, bucket: str, key: str, token: str) -> bytes:
    url = f"{STORAGE_API}/b/{bucket}/o/{quote(key, safe='')}?alt=media"
    reply = await fetch(url, {"authorization": f"Bearer {token}"})
    if reply.status != 200:
        raise WalkError(f"{key}: HTTP {reply.status}")
    return reply.body


async def read_snapshot(fetch: Fetch, bucket: str, org_id: str, token: str) -> SnapshotDoc:
    raw = await _object(fetch, bucket, latest_key(org_id), token)
    try:
        pointer = parse_pointer(raw, org_id)
    except SnapshotInvalidError as exc:
        raise WalkError(str(exc)) from None
    body = await _object(fetch, bucket, pointer.key, token)
    if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), pointer.sha256):
        raise WalkError(f"{pointer.key} does not match latest.json's digest")
    try:
        return SnapshotDoc.model_validate_json(body)
    except ValueError:
        raise WalkError(f"{pointer.key} is not an ssc-snapshot/v1 document") from None


async def host_failure(
    fetch: Fetch, label: str, suffix: str, *, current: str, auth_url: str
) -> str | None:
    host = f"{label}.{suffix}"
    try:
        reply = await fetch(f"https://{host}/", {})
    except WalkError as exc:
        return str(exc)
    location = reply.headers.get("location", "")
    got = f"{host}: {reply.status} to {location or '-'}"
    if suffix != current:
        want = f"https://{label}.{current}/"
        no_store = "no-store" in reply.headers.get("cache-control", "")
        if reply.status == 307 and location == want and no_store:
            return None
        return f"{got}, not a no-store 307 to {want}"
    if reply.status == 200 or (reply.status == 302 and location.startswith(f"{auth_url}/login")):
        return None
    return f"{got}, not 200 or 302 to login"


async def caa_failure(fetch: Fetch, suffix: str) -> str | None:
    try:
        reply = await fetch(f"{DOH}?name={suffix}&type=CAA", {"accept": "application/dns-json"})
    except WalkError as exc:
        return f"CAA {suffix}: {exc}"
    try:
        body: object = json.loads(reply.body)
    except ValueError:
        body = None
    if reply.status != 200 or not isinstance(body, dict):
        return f"CAA {suffix}: HTTP {reply.status}"
    answer = cast("dict[str, Any]", body).get("Answer")
    records = cast("list[object]", answer) if isinstance(answer, list) else []
    found = {
        " ".join(str(r.get("data", "")).split())
        for r in (cast("dict[str, Any]", x) for x in records if isinstance(x, dict))
        if r.get("type") == CAA_TYPE
    }
    return None if CAA in found else f"CAA {suffix}: {CAA} not answered"


async def certificate(days_left: DaysLeft, host: str) -> tuple[float | None, str | None]:
    try:
        days = await days_left(host)
    except NightlyError as exc:
        return None, f"certificate: {exc}"
    if days < MIN_DAYS:
        return days, f"certificate: {host} expires in {days:.0f} days, under {MIN_DAYS:.0f}"
    return days, None


@dataclass(frozen=True, slots=True)
class Walk:
    lines: tuple[str, ...]
    failures: tuple[str, ...]

    def as_result(self) -> evidence.Result:
        if self.failures:
            return evidence.Result(SUFFIXES, evidence.FAIL, "; ".join(self.failures))
        return evidence.Result(SUFFIXES, evidence.OK, "; ".join(self.lines))


async def walk(  # noqa: PLR0913  (keyword-only)
    fetch: Fetch,
    doc: SnapshotDoc,
    *,
    base: str,
    auth_url: str,
    now: datetime,
    days_left: DaysLeft = certificate_days_left,
) -> Walk:
    current = doc.suffix or base
    previous = [p.suffix for p in doc.previous_suffixes or () if p.until > now]
    labels = sorted(h for h, env in doc.hosts.items() if doc.environments[env].status == "active")
    if not labels:
        return Walk((), ("no active app host to walk",))
    lines: list[str] = []
    failures: list[str] = []
    for suffix in (current, *previous):
        found = [
            p
            for label in labels
            if (p := await host_failure(fetch, label, suffix, current=current, auth_url=auth_url))
        ]
        days, cert = await certificate(days_left, f"{labels[0]}.{suffix}")
        found += [cert] if cert else []
        caa = "not asked" if suffix == base else "present"
        if suffix != base and (problem := await caa_failure(fetch, suffix)):
            found.append(problem)
        failures += found
        role = "current" if suffix == current else "previous"
        answer = "answer" if suffix == current else "redirect"
        left = "-" if days is None else f"{days:.0f}"
        lines.append(
            f"{suffix} ({role}): {len(labels)} hosts {answer}, certificate {left} days, CAA {caa}"
        )
    return Walk(tuple(lines), tuple(failures))


async def main_async(
    environ: Mapping[str, str],
    *,
    fetch: Fetch | None = None,
    days_left: DaysLeft = certificate_days_left,
) -> Walk:
    cfg = config_from_env(environ)
    token = await gcloud_access_tokens()()
    async with httpx2.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        get = fetch or http_fetch(client)
        try:
            doc = await read_snapshot(get, cfg.bucket, cfg.org_id, token)
            result = await walk(
                get,
                doc,
                base=cfg.base,
                auth_url=cfg.auth_url,
                now=datetime.now(UTC),
                days_left=days_left,
            )
        except WalkError as exc:
            result = Walk((), (f"snapshot: {exc}",))
    if path := environ.get(evidence.EVIDENCE_ENV):
        evidence.write(Path(path), evidence.Evidence(cfg.project, False, (result.as_result(),)))
    return result


def main() -> int:
    try:
        result = asyncio.run(main_async(os.environ))
    except (WalkError, NightlyError) as exc:
        sys.stderr.write(f"suffix_walk: {exc}\n")
        return 1
    text = "\n".join(
        [*(f"- {line}" for line in result.lines), *(f"- FAILED {f}" for f in result.failures)]
    )
    sys.stdout.write(text + "\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as f:
            f.write("## CD-7 suffix walk\n\n" + text + "\n")
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main())
