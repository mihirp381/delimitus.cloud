"""An in-memory WorkOS for the SSC-019 tests, served through ``httpx2.MockTransport``.

Only the endpoints ``ssc_control.identity.workos`` calls. Lists page two items at a time so the
client's ``after`` loop runs.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx2

from ssc_control.identity.workos import WorkOSClient

PAGE = 2


def _id(prefix: str) -> str:
    return f"{prefix}_01{uuid.uuid4().hex.upper()}"


type Json = dict[str, Any]


@dataclass
class FakeWorkOS:
    """Fresh WorkOS ids per instance: the control database keeps them unique across orgs."""

    organization: str = field(default_factory=lambda: _id("org"))
    directory: str = field(default_factory=lambda: _id("directory"))
    sso: str = field(default_factory=lambda: _id("conn"))
    users: dict[str, Json] = field(default_factory=dict)
    groups: dict[str, str] = field(default_factory=dict)
    members: dict[str, set[str]] = field(default_factory=dict)
    events: list[Json] = field(default_factory=list)
    profiles: dict[str, Json] = field(default_factory=dict)
    fail: int | None = None
    calls: list[str] = field(default_factory=list)
    domains: dict[str, str] = field(default_factory=dict)
    """This organisation's domains and their state (``verified``, ``pending``...)."""

    # ── arranging ─────────────────────────────────────────────────────────────

    def user(  # noqa: PLR0913
        self,
        uid: str,
        idp_id: str,
        email: str,
        *,
        first: str = "Pat",
        last: str = "Doe",
        state: str = "active",
        **extra: Any,
    ) -> Json:
        raw = {
            "id": uid,
            "idp_id": idp_id,
            "directory_id": self.directory,
            "email": email,
            "first_name": first,
            "last_name": last,
            "state": state,
            **extra,
        }
        self.users[uid] = raw
        return raw

    def group(self, gid: str, name: str, *uids: str) -> None:
        self.groups[gid] = name
        self.members[gid] = set(uids)

    def event(self, kind: str, data: Json) -> None:
        self.events.append({"id": f"event_{len(self.events) + 1:06d}", "event": kind, "data": data})

    def user_event(self, kind: str, uid: str, idp_id: str | None = None) -> None:
        idp = idp_id if idp_id is not None else self.users[uid]["idp_id"]
        self.event(kind, {"id": uid, "idp_id": idp, "directory_id": self.directory})

    def profile(  # noqa: PLR0913
        self,
        code: str,
        idp_id: str,
        email: str,
        *,
        organization_id: str | None = None,
        connection_id: str | None = None,
        connection_type: str = "OktaSAML",
    ) -> None:
        self.profiles[code] = {
            "id": f"prof_{code}",
            "idp_id": idp_id,
            "organization_id": organization_id or self.organization,
            "connection_id": connection_id or self.sso,
            "connection_type": connection_type,
            "email": email,
            "first_name": "Pat",
            "last_name": "Doe",
        }

    # ── serving ───────────────────────────────────────────────────────────────

    def client(self) -> WorkOSClient:
        return WorkOSClient(
            api_key="sk_test_fake",
            client_id="client_fake",
            base="https://workos.test",
            transport=httpx2.MockTransport(self.handle),
        )

    def _page(self, items: list[Json], q: dict[str, list[str]]) -> httpx2.Response:
        start = int((q.get("after") or ["0"])[0])
        chunk = items[start : start + PAGE]
        after = str(start + PAGE) if start + PAGE < len(items) else None
        return httpx2.Response(200, json={"data": chunk, "list_metadata": {"after": after}})

    def handle(self, request: httpx2.Request) -> httpx2.Response:  # noqa: PLR0911
        path, q = request.url.path, parse_qs(request.url.query.decode())
        self.calls.append(path)
        if self.fail is not None:
            return httpx2.Response(self.fail)
        if path == "/sso/token":
            body = json.loads(request.content)
            profile = self.profiles.get(body.get("code"))
            if profile is None or body.get("client_secret") != "sk_test_fake":
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return httpx2.Response(200, json={"profile": profile, "access_token": "x"})
        if request.headers.get("authorization") != "Bearer sk_test_fake":
            return httpx2.Response(401)
        if path.startswith("/directory_users/"):
            raw = self.users.get(path.rsplit("/", 1)[1])
            return httpx2.Response(404) if raw is None else httpx2.Response(200, json=raw)
        if path == "/directory_users":
            if "group" in q:
                gid = q["group"][0]
                if gid not in self.groups:
                    return httpx2.Response(404)
                uids = sorted(self.members[gid])
            else:
                uids = sorted(self.users)
            return self._page([self.users[u] for u in uids if u in self.users], q)
        if path == "/directory_groups":
            gids = sorted(self.groups)
            if "user" in q:
                gids = [g for g in gids if q["user"][0] in self.members[g]]
            return self._page([{"id": g, "name": self.groups[g]} for g in gids], q)
        if path == "/organizations":
            wanted = set(q.get("domains") or [])
            mine = [{"domain": d, "state": s} for d, s in self.domains.items()]
            hit = any(d in wanted for d in self.domains)
            data = [{"id": self.organization, "name": "Acme", "domains": mine}] if hit else []
            return httpx2.Response(200, json={"data": data, "list_metadata": {"after": None}})
        if path == "/events":
            after = (q.get("after") or [""])[0]
            ids = [e["id"] for e in self.events]
            start = ids.index(after) + 1 if after in ids else 0
            return httpx2.Response(200, json={"data": self.events[start : start + PAGE]})
        return httpx2.Response(404)
