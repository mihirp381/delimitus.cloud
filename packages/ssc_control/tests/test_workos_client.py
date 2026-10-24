"""The WorkOS client's organisation, Admin Portal, connection and directory calls (ticket 19C),
against the reference shapes in fixtures/workos (README there names the sources)."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest
from fake_workos import FakeWorkOS

from ssc_control.identity.workos import (
    AmbiguousEmailError,
    Connection,
    Directory,
    WorkOSClient,
    WorkOSError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "workos"
KEY = "sk_test_client"
ORG = "org_01JTESTORGANISATION000000001"
DIRECTORY = "directory_01JTESTDIRECTORY0000000001"
SECRET_BODY = "pat.admin@acme.example in a body"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class Recorder:
    def __init__(self, *answers: httpx2.Response) -> None:
        self.answers = list(answers)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.answers.pop(0)

    def client(self) -> WorkOSClient:
        return WorkOSClient(
            api_key=KEY,
            client_id="client_test",
            base="https://workos.test",
            transport=httpx2.MockTransport(self),
        )

    @property
    def only(self) -> httpx2.Request:
        (request,) = self.requests
        return request


def query(request: httpx2.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode())


def listing(data: list[Any], after: str | None = None) -> httpx2.Response:
    return httpx2.Response(200, json={"data": data, "list_metadata": {"after": after}})


async def test_create_organization_claims_the_domain_without_verifying_it() -> None:
    rec = Recorder(httpx2.Response(201, json=fixture("organization_create.json")))
    assert await rec.client().create_organization(name="Acme", domain="acme.example") == ORG
    r = rec.only
    assert (r.method, r.url.path) == ("POST", "/organizations")
    assert r.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(r.content) == {
        "name": "Acme",
        "domain_data": [{"domain": "acme.example", "state": "pending"}],
    }


@pytest.mark.parametrize("intent", ["sso", "dsync"])
async def test_portal_link_asks_for_the_intent(intent: str) -> None:
    rec = Recorder(httpx2.Response(200, json=fixture("portal_link.json")))
    link = await rec.client().portal_link(organization=ORG, intent=intent)  # pyright: ignore[reportArgumentType]
    assert link == fixture("portal_link.json")["link"]
    r = rec.only
    assert (r.method, r.url.path) == ("POST", "/portal/generate_link")
    assert r.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(r.content) == {"organization": ORG, "intent": intent}


async def test_connections_keeps_id_type_and_state_across_pages() -> None:
    first, second = fixture("connections_list.json")["data"]
    rec = Recorder(listing([first], after="conn_cursor"), listing([second]))
    found = await rec.client().connections(ORG)
    assert found == [
        Connection("conn_01JTESTCONNECTION00000000001", "OktaSAML", "active"),
        Connection("conn_01JTESTCONNECTION00000000002", "GoogleOAuth", "draft"),
    ]
    for r in rec.requests:
        assert (r.method, r.url.path) == ("GET", "/connections")
        assert r.headers["authorization"] == f"Bearer {KEY}"
        assert query(r)["organization_id"] == [ORG]
    assert query(rec.requests[1])["after"] == ["conn_cursor"]


async def test_directories_keeps_id_type_and_state() -> None:
    rec = Recorder(httpx2.Response(200, json=fixture("directories_list.json")))
    found = await rec.client().directories(ORG)
    assert found == [Directory(DIRECTORY, "okta scim v2.0", "linked")]
    r = rec.only
    assert (r.method, r.url.path) == ("GET", "/directories")
    assert r.headers["authorization"] == f"Bearer {KEY}"
    assert query(r)["organization_id"] == [ORG]


async def test_directory_user_by_email_filters_on_the_directory_and_ignores_case() -> None:
    rec = Recorder(httpx2.Response(200, json=fixture("directory_users_by_email.json")))
    user = await rec.client().directory_user_by_email(DIRECTORY, "pat.admin@ACME.example")
    assert user is not None
    assert user["id"] == "directory_user_01JTESTDIRECTORYUSER00001"
    r = rec.only
    assert (r.method, r.url.path) == ("GET", "/directory_users")
    assert r.headers["authorization"] == f"Bearer {KEY}"
    assert query(r)["directory"] == [DIRECTORY]
    assert query(r)["email"] == ["pat.admin@ACME.example"]


async def test_directory_user_by_email_is_none_when_nobody_has_it() -> None:
    other = {**fixture("directory_users_by_email.json")["data"][0], "email": "sam@acme.example"}
    for answer in (listing([]), listing([other])):
        rec = Recorder(answer)
        assert await rec.client().directory_user_by_email(DIRECTORY, "pat@acme.example") is None


async def test_directory_user_by_email_refuses_two_holders() -> None:
    (user,) = fixture("directory_users_by_email.json")["data"]
    twin = {
        **user,
        "id": "directory_user_01JTESTDIRECTORYUSER00002",
        "email": "PAT.ADMIN@acme.example",
    }
    rec = Recorder(listing([user, twin]))
    with pytest.raises(AmbiguousEmailError) as e:
        await rec.client().directory_user_by_email(DIRECTORY, "pat.admin@acme.example")
    assert "@" not in str(e.value)


type Call = Callable[[WorkOSClient], Awaitable[object]]
CALLS: list[tuple[str, Call]] = [
    ("/organizations", lambda c: c.create_organization(name="Acme", domain="acme.example")),
    ("/portal/generate_link", lambda c: c.portal_link(organization=ORG, intent="dsync")),
    ("/connections", lambda c: c.connections(ORG)),
    ("/directories", lambda c: c.directories(ORG)),
    ("/directory_users", lambda c: c.directory_user_by_email(DIRECTORY, "pat@acme.example")),
]


@pytest.mark.parametrize("status", [401, 404, 409, 422, 500])
@pytest.mark.parametrize(("path", "call"), CALLS, ids=[p for p, _ in CALLS])
async def test_a_non_2xx_answer_names_the_status_and_path_but_never_the_body(
    path: str, call: Call, status: int
) -> None:
    rec = Recorder(httpx2.Response(status, text=SECRET_BODY))
    with pytest.raises(WorkOSError) as e:
        await call(rec.client())
    assert (e.value.status, e.value.path) == (status, path)
    assert SECRET_BODY not in str(e.value)
    assert "acme" not in str(e.value)


async def test_the_fake_serves_the_onboarding_calls() -> None:
    wo = FakeWorkOS()
    wo.user("directory_user_01ADMIN", "00uadmin", "Admin@acme.example")
    wo.connection_list.append({"id": wo.sso, "connection_type": "OktaSAML", "state": "active"})
    client = wo.client()
    try:
        org = await client.create_organization(name="Acme", domain="acme.example")
        assert org.startswith("org_")
        assert wo.created == {org: "acme.example"}
        link = await client.portal_link(organization=org, intent="sso")
        assert link == f"https://setup.workos.test/sso/{org}"
        assert await client.connections(org) == [Connection(wo.sso, "OktaSAML", "active")]
        assert await client.directories(org) == []
        user = await client.directory_user_by_email(wo.directory, "admin@ACME.example")
        assert user is not None
        assert user["id"] == "directory_user_01ADMIN"
    finally:
        await client.aclose()
    assert wo.calls == [
        "/organizations",
        "/portal/generate_link",
        "/connections",
        "/directories",
        "/directory_users",
    ]
