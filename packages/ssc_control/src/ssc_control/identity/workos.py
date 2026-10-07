"""A thin async client for the WorkOS endpoints SSC-019 uses (REST, no SDK, as in SSC-002).

SSO: ``GET /sso/authorize`` (built here, the browser goes there), ``POST /sso/token``.
Directory Sync: ``/directory_users``, ``/directory_users/{id}``, ``/directory_groups``.
Events: ``GET /events``, read as triggers only; the current state is always fetched again.
Organizations: ``GET /organizations?domains=``, to find a sign-in from a work email.

Every list is paged with ``after`` until WorkOS returns no cursor. Non-2xx answers raise
:class:`WorkOSError` carrying only the status and path, never the body (it can hold PII).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast
from urllib.parse import urlencode

import httpx2

from ssc_control.identity.rules import ProfileError, SsoProfile

DEFAULT_BASE: Final = "https://api.workos.com"
PAGE: Final = 100
ORG_LOOKUP: Final = 10
VERIFIED_DOMAIN_STATES: Final = frozenset({"verified", "legacy_verified"})
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

    def authorize_url(self, *, organization: str, redirect_uri: str, state: str) -> str:
        query = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "organization": organization,
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


def _verified(entry: Json, domain: str) -> bool:
    name = entry.get("domain")
    state = entry.get("state")
    return (
        isinstance(name, str)
        and name.lower() == domain
        and (state is None or state in VERIFIED_DOMAIN_STATES)
    )
