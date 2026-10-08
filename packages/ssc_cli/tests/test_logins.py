"""``ssc logins``: list unlinked logins and link one to a person (SSC-019, decision 024, GA-2.5)."""

import json

import httpx2
import pytest

from ssc_cli.credentials import SERVICE
from ssc_cli.errors import ExitCode
from ssc_cli.shapes import LoginLinkedResult, UnlinkedLoginsResult

ULG = "ulg_aaaaaaaaaaaaaaaaaaaa"
USR = "usr_aaaaaaaaaaaaaaaaaaaa"
OTHER = "usr_bbbbbbbbbbbbbbbbbbbb"
AT = "2026-10-08T00:00:00Z"


def _unlinked(**changes: object) -> dict[str, object]:
    return {
        "id": ULG,
        "connection_id": "conn_01ABC",
        "subject": "00u1abcd",
        "email": "ada@example.com",
        "reason": "no_match",
        "attempts": 2,
        "first_seen_at": AT,
        "last_seen_at": AT,
        "linkable": True,
    } | changes


def _user(user_id: str = USR, status: str = "active") -> dict[str, object]:
    return {
        "id": user_id,
        "display_name": "Ada",
        "email": "ada@example.com",
        "role": "member",
        "status": status,
    }


@pytest.fixture
def scripted(fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    return fake_api


def test_list_shows_each_login_and_whether_it_can_be_linked(cli, scripted):
    body = {"unlinked_logins": [_unlinked(), _unlinked(id="ulg_b", linkable=False)]}
    scripted.add("GET", "/v1/unlinked-logins", httpx2.Response(200, json=body))
    r = cli("logins", "list", "--json", session=scripted.session())
    assert r.code == 0, r.stderr
    result = UnlinkedLoginsResult.model_validate(r.json())
    assert [(u.id, u.linkable) for u in result.unlinked_logins] == [(ULG, True), ("ulg_b", False)]
    scripted.add("GET", "/v1/unlinked-logins", httpx2.Response(200, json=body))
    human = cli("logins", "list", session=scripted.session())
    assert "ada@example.com" in human.stdout
    assert "fix it in the directory" in human.stdout
    assert "ssc logins link" in human.stdout


def test_an_empty_list_says_so(cli, scripted):
    scripted.add("GET", "/v1/unlinked-logins", httpx2.Response(200, json={"unlinked_logins": []}))
    r = cli("logins", "list", session=scripted.session())
    assert r.stdout.strip() == "Every login so far matched a person."


def test_link_by_id_sends_the_person(cli, scripted):
    done = {"identity_link_id": "idl_a", "user_id": USR}
    scripted.add("POST", f"/v1/unlinked-logins/{ULG}/link", httpx2.Response(200, json=done))
    r = cli("logins", "link", ULG, "--to", USR, "--json", session=scripted.session())
    assert r.code == 0, r.stderr
    assert json.loads(scripted.seen[-1].content) == {"user_id": USR}
    assert scripted.seen[-1].headers["idempotency-key"]
    result = LoginLinkedResult.model_validate(r.json())
    assert (result.unlinked_login_id, result.user_id) == (ULG, USR)


def test_link_by_email_picks_the_one_active_person(cli, scripted):
    found = {"users": [_user(OTHER, "deactivated"), _user()]}
    scripted.add("GET", "/v1/users", httpx2.Response(200, json=found))
    done = {"identity_link_id": "idl_a", "user_id": USR}
    scripted.add("POST", f"/v1/unlinked-logins/{ULG}/link", httpx2.Response(200, json=done))
    r = cli("logins", "link", ULG, "--to", "ada@example.com", session=scripted.session())
    assert r.code == 0, r.stderr
    assert json.loads(scripted.seen[-1].content) == {"user_id": USR}
    assert f"Linked {ULG} to {USR}" in r.stdout


@pytest.mark.parametrize(
    ("users", "code"),
    [([], "USER_NOT_FOUND"), ([_user(), _user(OTHER)], "SUBJECT_AMBIGUOUS")],
)
def test_link_by_email_needs_exactly_one_active_person(cli, scripted, users, code):
    scripted.add("GET", "/v1/users", httpx2.Response(200, json={"users": users}))
    r = cli("logins", "link", ULG, "--to", "ada@example.com", "--json", session=scripted.session())
    assert r.code != 0
    assert r.json()["error"]["code"] == code
    assert all(s.method == "GET" for s in scripted.seen)


@pytest.mark.parametrize(
    ("status", "code"),
    [(403, "FORBIDDEN"), (404, "NOT_FOUND"), (422, "VALIDATION_FAILED")],
)
def test_a_refusal_is_shown_with_its_code(cli, scripted, fake_problem, status, code):
    scripted.add("POST", f"/v1/unlinked-logins/{ULG}/link", fake_problem(status, code))
    r = cli("logins", "link", ULG, "--to", USR, "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == code
