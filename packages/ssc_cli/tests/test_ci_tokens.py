"""``ssc token create-ci``, ``list-ci`` and ``revoke-ci`` (GA-7.7): a preview-scoped CI token for
a repository secret, made at the auth host from the person's own login and shown once."""

import json
import time

import httpx2
import pytest

from ssc_cli.credentials import SERVICE, Login, store_login
from ssc_cli.errors import ExitCode
from ssc_cli.shapes import CiTokenCreated, CiTokenRevoked, CiTokensResult, ErrorResult

API = "https://api.test"
AUTH = "https://auth.test"
ORG = "org_aaaaaaaaaaaaaaaaaaaa"
USR = "usr_aaaaaaaaaaaaaaaaaaaa"
CI_ID = "ses_cicicicicicicicicici"
WHOAMI = {
    "org_id": ORG,
    "subject": USR,
    "kind": "user",
    "credential_id": "ses_1",
    "is_agent": False,
    "client_id": None,
}
ISSUED = {
    "token": "ci-access-token",
    "id": CI_ID,
    "label": "acme/web",
    "expires_at": "2027-01-07T12:00:00Z",
}


def row(n: int, *, revoked_at: str | None = None, expires_at: str = "2099-01-01T00:00:00Z"):
    return {
        "id": f"ses_{n:0>20}",
        "user_id": USR,
        "label": f"repo-{n}",
        "created_at": "2026-10-09T12:00:00Z",
        "expires_at": expires_at,
        "revoked_at": revoked_at,
    }


@pytest.fixture
def signed_in(isolated):
    store_login(
        API,
        Login(
            auth_url=AUTH,
            org_id=ORG,
            access_token="access-1",
            expires_at=int(time.time()) + 300,
            refresh_token=f"ssc_rt.{ORG}.refresh-1",
        ),
    )
    return isolated


def test_create_ci_shows_the_token_once_and_keeps_nothing(cli, fake_api, signed_in):
    before = dict(signed_in.store)
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    fake_api.add("POST", "/ci-tokens", httpx2.Response(200, json=ISSUED))
    r = cli("token", "create-ci", "--label", "acme/web", "--json", session=fake_api.session())
    assert r.code == 0, r.stderr
    assert CiTokenCreated.model_validate(r.json()) == CiTokenCreated(
        api_url=API, auth_url=AUTH, **ISSUED
    )
    whoami, created = fake_api.seen
    assert whoami.headers["authorization"] == "Bearer access-1"
    assert str(created.url) == f"{AUTH}/ci-tokens"
    assert created.headers["authorization"] == "Bearer access-1"
    assert json.loads(created.content) == {"label": "acme/web", "days": 90}
    assert signed_in.store == before


def test_create_ci_prints_the_token_alone_on_stdout(cli, fake_api, signed_in):
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    fake_api.add("POST", "/ci-tokens", httpx2.Response(200, json=ISSUED))
    args = ("token", "create-ci", "--label", "acme/web", "--days", "7")
    r = cli(*args, session=fake_api.session())
    assert r.code == 0, r.stderr
    assert r.stdout == "ci-access-token\n"
    for said in ("shown once", "SSC_PREVIEW_TOKEN", "never touches prod", ISSUED["expires_at"]):
        assert said in r.stderr
    assert f"ssc token revoke-ci {CI_ID}" in r.stderr
    assert "ci-access-token" not in r.stderr
    assert json.loads(fake_api.seen[1].content)["days"] == 7


def test_create_ci_takes_auth_url(cli, fake_api, signed_in):
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    fake_api.add("POST", "/ci-tokens", httpx2.Response(200, json=ISSUED))
    args = ("token", "create-ci", "--label", "x", "--auth-url", "https://login.test", "--json")
    r = cli(*args, session=fake_api.session())
    assert r.code == 0, r.stderr
    assert r.json()["auth_url"] == "https://login.test"
    assert str(fake_api.seen[1].url) == "https://login.test/ci-tokens"


@pytest.mark.parametrize("days", ["0", "91"])
def test_create_ci_days_are_1_to_90(cli, fake_api, signed_in, days):
    args = ("token", "create-ci", "--label", "x", "--days", days)
    r = cli(*args, session=fake_api.session())
    assert r.code == ExitCode.USAGE
    assert fake_api.seen == []


def test_create_ci_refuses_an_agent_login(cli, fake_api, isolated):
    isolated.set_password(SERVICE, API, "agent-access")
    agent = {**WHOAMI, "is_agent": True, "client_id": "claude-code"}
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=agent))
    r = cli("token", "create-ci", "--label", "x", "--json", session=fake_api.session())
    assert r.code == ExitCode.AUTH
    assert ErrorResult.model_validate(r.json()).error.code == "CI_TOKEN_REFUSED"
    assert [s.url.path for s in fake_api.seen] == ["/v1/whoami"]


@pytest.mark.parametrize(
    ("status", "error", "exit_code"),
    [
        (400, "invalid_request", ExitCode.USAGE),
        (401, "invalid_token", ExitCode.AUTH),
        (403, "access_denied", ExitCode.AUTH),
        (429, "slow_down", ExitCode.FAILED),
    ],
)
def test_create_ci_refused_by_the_auth_host(  # noqa: PLR0913
    cli, fake_api, signed_in, status, error, exit_code
):
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    refused = httpx2.Response(status, json={"error": error}, headers={"Retry-After": "3600"})
    fake_api.add("POST", "/ci-tokens", refused)
    r = cli("token", "create-ci", "--label", "x", "--json", session=fake_api.session())
    assert r.code == exit_code
    assert ErrorResult.model_validate(r.json()).error.code == "CI_TOKEN_REFUSED"
    assert [s.url.path for s in fake_api.seen] == ["/v1/whoami", "/ci-tokens"]


def test_list_ci(cli, fake_api, isolated):
    isolated.set_password(SERVICE, API, "access")
    rows = [
        row(1),
        row(2, revoked_at="2026-10-09T13:00:00Z"),
        row(3, expires_at="2026-01-01T00:00:00Z"),
    ]
    fake_api.add("GET", "/v1/ci-tokens", httpx2.Response(200, json={"ci_tokens": rows}))
    r = cli("token", "list-ci", "--json", session=fake_api.session())
    assert r.code == 0, r.stderr
    assert CiTokensResult.model_validate(r.json()).model_dump() == {
        "api_url": API,
        "ci_tokens": rows,
    }
    human = cli("token", "list-ci", session=fake_api.session())
    assert human.code == 0, human.stderr
    lines = human.stdout.splitlines()
    assert lines[0].split() == ["ID", "LABEL", "OWNER", "CREATED", "EXPIRES", "STATE"]
    assert [line.split()[-1] for line in lines[-3:]] == ["live", "revoked", "expired"]
    assert "ci-access-token" not in human.stdout


def test_list_ci_says_when_there_are_none(cli, fake_api, isolated):
    isolated.set_password(SERVICE, API, "access")
    fake_api.add("GET", "/v1/ci-tokens", httpx2.Response(200, json={"ci_tokens": []}))
    r = cli("token", "list-ci", session=fake_api.session())
    assert r.code == 0 and "No CI tokens" in r.stdout


def test_revoke_ci(cli, fake_api, isolated):
    isolated.set_password(SERVICE, API, "access")
    done = row(1, revoked_at="2026-10-09T13:00:00Z")
    fake_api.add("DELETE", f"/v1/ci-tokens/{done['id']}", httpx2.Response(200, json=done))
    r = cli("token", "revoke-ci", done["id"], "--json", session=fake_api.session())
    assert r.code == 0, r.stderr
    assert CiTokenRevoked.model_validate(r.json()) == CiTokenRevoked(
        api_url=API, id=done["id"], revoked_at=done["revoked_at"]
    )
    human = cli("token", "revoke-ci", done["id"], session=fake_api.session())
    assert human.code == 0 and f"Revoked {done['id']}" in human.stdout


def test_revoke_ci_of_someone_elses_is_not_found(cli, fake_api, fake_problem, isolated):
    isolated.set_password(SERVICE, API, "access")
    fake_api.add("DELETE", f"/v1/ci-tokens/{CI_ID}", fake_problem(404, "NOT_FOUND"))
    r = cli("token", "revoke-ci", CI_ID, "--json", session=fake_api.session())
    assert r.code == ExitCode.FAILED
    assert ErrorResult.model_validate(r.json()).error.code == "NOT_FOUND"
