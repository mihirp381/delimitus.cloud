"""A thin async client for the WorkOS endpoints SSC-019 uses (REST, no SDK, as in SSC-002).

SSO: ``GET /sso/authorize`` (built here, the browser goes there), ``POST /sso/token``.
Directory Sync: ``/directory_users``, ``/directory_users/{id}``, ``/directory_groups``.
Events: ``GET /events``, read as triggers only; the current state is always fetched again.
Organizations: ``GET /organizations?domains=``, to find a sign-in from a work email.
Organizations: ``POST /organizations``, a new organisation with its domain claimed, not verified.
Admin Portal: ``POST /portal/generate_link``, a setup link for SSO or Directory Sync.
Connections: ``GET /connections?organization_id=``, the organisation's SSO connections.
Directories: ``GET /directories?organization_id=``, the organisation's directories.
Directory users: ``GET /directory_users?directory=&email=``, one person by primary email.

Every list is paged with ``after`` until WorkOS returns no cursor. Non-2xx answers raise
:class:`WorkOSError` carrying only the status and path, never the body (it can hold PII).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast
from urllib.parse import urlencode

import httpx2

from ssc_control.identity.rules import DirectoryPerson, ProfileError, SsoProfile

DEFAULT_BASE: Final = "https://api.workos.com"
PAGE: Final = 100
ORG_LOOKUP: Final = 10
VERIFIED_DOMAIN_STATES: Final = frozenset({"verified", "legacy_verified"})
CLAIMED_DOMAIN_STATE: Final = "pending"
TIMEOUT_SECONDS: Final = 20.0
DSYNC_EVENTS: Final = (
    "dsync.activated",
    "dsync.deleted",
    "dsync.user.created",
    "dsync.user.updated",
    "dsync.user.deleted",
    "dsync.group.created",
    "dsync.group.updated",
    "dsync.group.deleted",
    "dsync.group.user_added",
    "dsync.group.user_removed",
)

type Json = dict[str, Any]


class WorkOSError(RuntimeError):
    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"WorkOS answered HTTP {status} for {path}")
        self.status = status
        self.path = path


class AmbiguousEmailError(ValueError):
    """More than one directory user has the address."""


@dataclass(frozen=True, slots=True)
class Connection:
    id: str
    connection_type: str
    state: str


@dataclass(frozen=True, slots=True)
class Directory:
    id: str
    type: str
    state: str


@dataclass(frozen=True, slots=True)
class EventPage:
    events: tuple[Json, ...]
    last_id: str | None


class WorkOSClient:
    def __init__(
        self,
        *,
        api_key: str,
        client_id: str,
        base: str = DEFAULT_BASE,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._client_id = client_id
        self._base = base.rstrip("/")
        self._http = httpx2.AsyncClient(
            base_url=self._base, timeout=TIMEOUT_SECONDS, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def authorize_url(
        self, *, organization: str = "", provider: str = "", redirect_uri: str, state: str
    ) -> str:
        """Exactly one of ``organization`` and ``provider`` (a social connection)."""
        if bool(organization) == bool(provider):
            raise ValueError("authorize needs exactly one of organization and provider")
        target = {"organization": organization} if organization else {"provider": provider}
        query = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            **target,
            "state": state,
        }
        return f"{self._base}/sso/authorize?{urlencode(query)}"

    async def profile(self, code: str) -> SsoProfile:
        body = {
            "client_id": self._client_id,
            "client_secret": self._key,
            "grant_type": "authorization_code",
            "code": code,
        }
        r = await self._http.post("/sso/token", json=body)
        if r.status_code != 200:  # noqa: PLR2004
            raise WorkOSError(r.status_code, "/sso/token")
        raw = cast(Json, r.json())
        profile = raw.get("profile")
        if not isinstance(profile, dict):
            raise ProfileError("no profile")
        return SsoProfile.from_wire(cast(Json, profile))

    async def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Json | None:
        r = await self._http.get(
            path, params=params, headers={"authorization": f"Bearer {self._key}"}
        )
        if r.status_code == 404:  # noqa: PLR2004
            return None
        if r.status_code != 200:  # noqa: PLR2004
            raise WorkOSError(r.status_code, path)
        return cast(Json, r.json())

    async def _post(self, path: str, body: Json) -> Json:
        r = await self._http.post(path, json=body, headers={"authorization": f"Bearer {self._key}"})
        if not r.is_success:
            raise WorkOSError(r.status_code, path)
        return cast(Json, r.json())

    async def _all(self, path: str, params: Mapping[str, Any]) -> list[Json]:
        out: list[Json] = []
        after: str | None = None
        while True:
            q: dict[str, Any] = {**params, "limit": PAGE}
            if after:
                q["after"] = after
            page = await self._get(path, q)
            if page is None:
                raise WorkOSError(404, path)
            out.extend(cast(list[Json], page.get("data") or []))
            after = cast(Json, page.get("list_metadata") or {}).get("after")
            if not after:
                return out

    async def directory_users(self, directory: str) -> list[Json]:
        return await self._all("/directory_users", {"directory": directory})

    async def directory_user(self, user_id: str) -> Json | None:
        """None when WorkOS no longer has the user (removed from the directory)."""
        return await self._get(f"/directory_users/{user_id}")

    async def group_members(self, group_id: str) -> list[Json]:
        return await self._all("/directory_users", {"group": group_id})

    async def directory_groups(self, directory: str) -> list[Json]:
        return await self._all("/directory_groups", {"directory": directory})

    async def user_groups(self, user_id: str) -> list[Json]:
        return await self._all("/directory_groups", {"user": user_id})

    async def events(
        self, *, organization_id: str, after: str | None, kinds: Sequence[str] = DSYNC_EVENTS
    ) -> EventPage:
        """One page of the org's events after ``after``, oldest first."""
        q: dict[str, Any] = {"events": list(kinds), "organization_id": organization_id}
        q["limit"] = PAGE
        if after:
            q["after"] = after
        page = await self._get("/events", q)
        if page is None:
            raise WorkOSError(404, "/events")
        data = tuple(cast(list[Json], page.get("data") or []))
        last = data[-1].get("id") if data else None
        return EventPage(data, last if isinstance(last, str) else None)

    async def organizations_for_domain(self, domain: str) -> list[str]:
        """The WorkOS organisations that have verified ``domain`` (decision 029). A domain an
        organisation only claims, not verified, never counts: anyone can claim one."""
        page = await self._get("/organizations", {"domains": [domain], "limit": ORG_LOOKUP})
        if page is None:
            raise WorkOSError(404, "/organizations")
        found: list[str] = []
        for org in cast(list[Json], page.get("data") or []):
            org_id = org.get("id")
            domains = cast(list[Json], org.get("domains") or [])
            if isinstance(org_id, str) and any(_verified(d, domain) for d in domains):
                found.append(org_id)
        return found

    async def create_organization(self, *, name: str, domain: str) -> str:
        body = {"name": name, "domain_data": [{"domain": domain, "state": CLAIMED_DOMAIN_STATE}]}
        org_id = (await self._post("/organizations", body)).get("id")
        if not isinstance(org_id, str) or not org_id:
            raise ProfileError("no id")
        return org_id

    async def portal_link(self, *, organization: str, intent: Literal["sso", "dsync"]) -> str:
        body = {"organization": organization, "intent": intent}
        link = (await self._post("/portal/generate_link", body)).get("link")
        if not isinstance(link, str) or not link:
            raise ProfileError("no link")
        return link

    async def connections(self, organization: str) -> list[Connection]:
        rows = await self._all("/connections", {"organization_id": organization})
        return [
            Connection(*f) for r in rows if (f := _strings(r, "id", "connection_type", "state"))
        ]

    async def directories(self, organization: str) -> list[Directory]:
        rows = await self._all("/directories", {"organization_id": organization})
        return [Directory(*f) for r in rows if (f := _strings(r, "id", "type", "state"))]

    async def directory_user_by_email(self, directory: str, email: str) -> Json | None:
        wanted = email.strip().lower()
        rows = await self._all("/directory_users", {"directory": directory, "email": email.strip()})
        found: list[Json] = []
        for raw in rows:
            try:
                if DirectoryPerson.from_wire(raw).email == wanted:
                    found.append(raw)
            except ProfileError:
                continue
        if len(found) > 1:
            raise AmbiguousEmailError(
                f"{len(found)} users of directory {directory} have this email"
            )
        return found[0] if found else None


def _strings(raw: Json, *keys: str) -> tuple[str, ...] | None:
    values = tuple(raw.get(k) for k in keys)
    return cast(tuple[str, ...], values) if all(isinstance(v, str) and v for v in values) else None


def _verified(entry: Json, domain: str) -> bool:
    name = entry.get("domain")
    state = entry.get("state")
    return (
        isinstance(name, str)
        and name.lower() == domain
        and (state is None or state in VERIFIED_DOMAIN_STATES)
    )
