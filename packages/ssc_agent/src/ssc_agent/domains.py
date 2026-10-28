"""Customer domains in the cell (CD-3): a public Cloud DNS zone per suffix, a wildcard
certificate and its map entry, over the Cloud DNS v1 and Certificate Manager v1 APIs.

The agent's IAM reaches every zone and certificate in the project and has no zone delete, so its
code touches only resources named ``ssc-d-*`` (``ssc_contracts.domains.resource_name``).
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import httpx2

from ssc_agent.cloud_run import AccessTokens
from ssc_contracts.domains import RESOURCE_NAME, CertificateState, resource_name
from ssc_contracts.snapshot import check_suffix
from ssc_shared.redaction import redact

type Json = dict[str, Any]

DNS_API: Final = "https://dns.googleapis.com/dns/v1"
CM_API: Final = "https://certificatemanager.googleapis.com/v1"
CALL_TIMEOUT_SECONDS: Final = 30.0
OPERATION_TRIES: Final = 60
RECORD_TTL: Final = 300
KEPT_TYPES: Final = frozenset({"NS", "SOA"})
MAX_DETAIL: Final = 1000
_HTTP_NOT_FOUND: Final = 404
_HTTP_CONFLICT: Final = 409
_HTTP_BAD_REQUEST: Final = 400
_STATES: Final[Mapping[str, CertificateState]] = {"ACTIVE": "active", "FAILED": "failed"}
_ID_PARAMS: Final = {
    "dnsAuthorizations": "dnsAuthorizationId",
    "certificates": "certificateId",
    "certificateMapEntries": "certificateMapEntryId",
}
_MAP_ENTRIES: Final = "certificateMapEntries"


class DomainsError(Exception):
    """Cloud DNS or Certificate Manager refused or failed a call."""


class DomainNotFoundError(DomainsError):
    """The suffix has no zone or certificate in the cell."""


class _ApiError(DomainsError):
    def __init__(self, what: str, status: int, reason: str) -> None:
        super().__init__(f"{what}: HTTP {status} {reason}")
        self.status = status


@dataclass(frozen=True, slots=True, kw_only=True)
class Created:
    zone_name: str
    name_servers: tuple[str, ...]
    certificate: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Status:
    name_servers: tuple[str, ...]
    state: CertificateState
    detail: str | None


def check_resource(name: str) -> str:
    if RESOURCE_NAME.fullmatch(name) is None:
        raise DomainsError(f"not an SSC domain resource: {name!r}")
    return name


class CellDomains:
    def __init__(  # noqa: PLR0913
        self,
        project: str,
        tokens: AccessTokens,
        *,
        entry_ip: str,
        certificate_map: str,
        client: httpx2.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_seconds: float = 1.0,
    ) -> None:
        self.project = project
        self.entry_ip = entry_ip
        self.certificate_map = certificate_map
        self._tokens = tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)
        self._sleep = sleep
        self._poll = poll_seconds

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def _zones(self) -> str:
        return f"{DNS_API}/projects/{self.project}/managedZones"

    @property
    def _cm(self) -> str:
        return f"{CM_API}/projects/{self.project}/locations/global"

    def _zone(self, name: str) -> str:
        return f"{self._zones}/{check_resource(name)}"

    def _cm_path(self, kind: str, name: str) -> str:
        return f"projects/{self.project}/locations/global/{kind}/{check_resource(name)}"

    @property
    def _entries(self) -> str:
        return f"certificateMaps/{self.certificate_map}/{_MAP_ENTRIES}"

    async def create(self, suffix: str) -> Created:
        name = resource_name(suffix)
        zone = await self._ensure_zone(name, suffix)
        await self._ensure_records(name, _records(suffix, self.entry_ip))
        auth = await self._ensure(
            "dnsAuthorizations",
            name,
            {"domain": suffix, "description": suffix},
            lambda found: found.get("domain") == suffix,
        )
        record = _obj(auth.get("dnsResourceRecord"))
        cname = str(record.get("name") or "")
        if record.get("type") != "CNAME" or not cname.endswith(f".{suffix}."):
            raise DomainsError(f"{name}: the authorization's record is not in the zone")
        await self._ensure_records(name, [_rrset(cname, "CNAME", [str(record.get("data"))])])
        domains = sorted([f"*.{suffix}", suffix])
        certificate = self._cm_path("certificates", name)
        await self._ensure(
            "certificates",
            name,
            {
                "description": suffix,
                "managed": {
                    "domains": domains,
                    "dnsAuthorizations": [self._cm_path("dnsAuthorizations", name)],
                },
            },
            lambda found: sorted(_obj(found.get("managed")).get("domains") or []) == domains,
        )
        await self._ensure(
            self._entries,
            name,
            {"description": suffix, "hostname": f"*.{suffix}", "certificates": [certificate]},
            lambda found: found.get("hostname") == f"*.{suffix}",
        )
        return Created(
            zone_name=name,
            name_servers=tuple(str(ns) for ns in zone.get("nameServers") or ()),
            certificate=certificate,
        )

    async def status(self, suffix: str) -> Status:
        name = resource_name(suffix)
        zone = await self._get(self._zone(name))
        cert = await self._get(f"{CM_API}/{self._cm_path('certificates', name)}")
        if zone is None or cert is None:
            raise DomainNotFoundError(f"{suffix}: no zone or certificate")
        _check_zone(zone, suffix)
        managed = _obj(cert.get("managed"))
        return Status(
            name_servers=tuple(str(ns) for ns in zone.get("nameServers") or ()),
            state=_STATES.get(str(managed.get("state")), "provisioning"),
            detail=_detail(managed),
        )

    async def retire(self, suffix: str) -> None:
        """Delete the map entry, the certificate, the authorization and every record but NS and
        SOA. Never the zone."""
        name = resource_name(suffix)
        for path in (
            self._cm_path(self._entries, name),
            self._cm_path("certificates", name),
            self._cm_path("dnsAuthorizations", name),
        ):
            try:
                await self._wait(await self._call("DELETE", f"{CM_API}/{path}"))
            except _ApiError as exc:
                if exc.status != _HTTP_NOT_FOUND:
                    raise
        zone = await self._get(self._zone(name))
        if zone is None:
            return
        _check_zone(zone, suffix)
        doomed = [r for r in await self._rrsets(name) if r.get("type") not in KEPT_TYPES]
        if doomed:
            await self._change(name, additions=[], deletions=doomed)

    async def _ensure_zone(self, name: str, suffix: str) -> Json:
        body = {
            "name": name,
            "dnsName": f"{suffix}.",
            "description": suffix,
            "visibility": "public",
            "dnssecConfig": {"state": "off"},
        }
        try:
            zone = await self._call("POST", self._zones, json=body)
        except _ApiError as exc:
            if exc.status != _HTTP_CONFLICT:
                raise
            zone = await self._call("GET", self._zone(name))
        _check_zone(zone, suffix)
        return zone

    async def _ensure(
        self, kind: str, name: str, body: Json, matches: Callable[[Json], bool]
    ) -> Json:
        path = self._cm_path(kind, name)
        collection = path.rsplit("/", 1)[0]
        id_param = _ID_PARAMS[kind.rsplit("/", 1)[-1]]
        try:
            operation = await self._call(
                "POST", f"{CM_API}/{collection}", json=body, params={id_param: name}
            )
            await self._wait(operation)
        except _ApiError as exc:
            if exc.status != _HTTP_CONFLICT:
                raise
        found = await self._call("GET", f"{CM_API}/{path}")
        if not matches(found):
            raise DomainsError(f"{path} exists for something else")
        return found

    async def _wait(self, operation: Json) -> None:
        for _ in range(OPERATION_TRIES):
            if operation.get("done"):
                error = _obj(operation.get("error"))
                if error:
                    raise DomainsError(f"operation failed: {error.get('message', '')}")
                return
            name = str(operation.get("name") or "")
            if not name.startswith(f"projects/{self.project}/locations/global/operations/"):
                raise DomainsError("not an operation of this project")
            await self._sleep(self._poll)
            operation = await self._call("GET", f"{CM_API}/{name}")
        raise DomainsError(f"operation not done after {OPERATION_TRIES} polls")

    async def _ensure_records(self, zone: str, wanted: Sequence[Json]) -> None:
        present = {(r.get("name"), r.get("type")): r for r in await self._rrsets(zone)}
        additions: list[Json] = []
        deletions: list[Json] = []
        for record in wanted:
            old = present.get((record["name"], record["type"]))
            if old is not None and _same(old, record):
                continue
            if old is not None:
                deletions.append(old)
            additions.append(record)
        if additions:
            await self._change(zone, additions=additions, deletions=deletions)

    async def _change(self, zone: str, *, additions: list[Json], deletions: list[Json]) -> None:
        body = {"additions": additions, "deletions": deletions}
        await self._call("POST", f"{self._zone(zone)}/changes", json=body)

    async def _rrsets(self, zone: str) -> list[Json]:
        found: list[Json] = []
        token = ""
        while True:
            params = {"pageToken": token} if token else {}
            page = await self._call("GET", f"{self._zone(zone)}/rrsets", params=params)
            found.extend(_objs(page.get("rrsets")))
            token = str(page.get("nextPageToken") or "")
            if not token:
                return found

    async def _get(self, url: str) -> Json | None:
        try:
            return await self._call("GET", url)
        except _ApiError as exc:
            if exc.status == _HTTP_NOT_FOUND:
                return None
            raise

    async def _call(
        self,
        method: Literal["GET", "POST", "DELETE"],
        url: str,
        *,
        json: Json | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Json:
        what = f"{method} {url.removeprefix(DNS_API).removeprefix(CM_API)}"
        headers = {"Authorization": f"Bearer {await self._tokens()}"}
        try:
            response = await self._client.request(
                method, url, json=json, params=dict(params or {}), headers=headers
            )
        except httpx2.HTTPError as exc:
            raise DomainsError(f"{what}: {type(exc).__name__}") from None
        if response.status_code >= _HTTP_BAD_REQUEST:
            raise _ApiError(what, response.status_code, _reason(response))
        if not response.content:
            return {}
        return _obj(response.json())


def _records(suffix: str, entry_ip: str) -> list[Json]:
    apex = f"{suffix}."
    return [
        _rrset(f"*.{apex}", "A", [entry_ip]),
        _rrset(apex, "CAA", ['0 issue "pki.goog"']),
        _rrset(apex, "MX", ["0 ."]),
        _rrset(apex, "TXT", ['"v=spf1 -all"']),
        _rrset(f"_dmarc.{apex}", "TXT", ['"v=DMARC1; p=reject"']),
    ]


def _rrset(name: str, kind: str, rrdatas: list[str]) -> Json:
    return {"name": name, "type": kind, "ttl": RECORD_TTL, "rrdatas": rrdatas}


def _same(old: Json, new: Json) -> bool:
    return old.get("ttl") == new["ttl"] and old.get("rrdatas") == new["rrdatas"]


def _check_zone(zone: Json, suffix: str) -> None:
    check_resource(str(zone.get("name") or ""))
    if zone.get("dnsName") != f"{check_suffix(suffix)}.":
        raise DomainsError(f"zone {zone.get('name')} is not the zone of {suffix}")


def _detail(managed: Json) -> str | None:
    parts: list[str] = []
    issue = _obj(managed.get("provisioningIssue"))
    if issue:
        parts.append(f"{issue.get('reason', '')} {issue.get('details', '')}".strip())
    for attempt in _objs(managed.get("authorizationAttemptInfo")):
        if attempt.get("state") == "AUTHORIZED":
            continue
        why = attempt.get("failureReason") or attempt.get("state") or ""
        parts.append(f"{attempt.get('domain', '')}: {why} {attempt.get('details', '')}".strip())
    return redact("; ".join(parts))[:MAX_DETAIL] or None


def _obj(value: object) -> Json:
    return cast(Json, value) if isinstance(value, dict) else {}


def _objs(value: object) -> list[Json]:
    items = cast("list[object]", value) if isinstance(value, list) else []
    return [cast(Json, v) for v in items if isinstance(v, dict)]


def _reason(response: httpx2.Response) -> str:
    try:
        error = _obj(_obj(response.json()).get("error"))
    except ValueError:
        return response.reason_phrase
    return redact(f"{error.get('status', '')} {error.get('message', '')}".strip())


__all__ = [
    "CellDomains",
    "Created",
    "DomainNotFoundError",
    "DomainsError",
    "Status",
    "check_resource",
]
