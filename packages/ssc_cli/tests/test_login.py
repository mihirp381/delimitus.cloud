"""``ssc login`` (device flow), refreshing a kept login, and ``ssc logout`` (SSC-019), and the
login kept for a coding agent (``--agent``, SSC-048)."""

import time
from urllib.parse import parse_qs

import httpx2
import pytest

from ssc_cli.credentials import (
    SERVICE,
    Login,
    agent_bearer,
    bearer,
    entry,
    read_login,
    store_login,
)
from ssc_cli.errors import CliError, ExitCode
from ssc_cli.login import auth_url_for
from ssc_cli.session import Session
from ssc_cli.shapes import ErrorResult, LoginResult, LogoutResult

API = "https://api.test"
AUTH = "https://auth.test"
WHOAMI = {
    "org_id": "org_aaaaaaaaaaaaaaaaaaaa",
    "subject": "usr_aaaaaaaaaaaaaaaaaaaa",
    "kind": "user",
    "credential_id": "ses_1",
    "is_agent": False,
    "client_id": None,
}
ORG = WHOAMI["org_id"]
START = {
    "device_code": f"{ORG}.devicesecret",
    "user_code": "BCDFGHJK",
    "verification_uri": f"{AUTH}/device?org={ORG}",
    "verification_uri_complete": f"{AUTH}/device?org={ORG}&user_code=BCDFGHJK",
    "expires_in": 600,
    "interval": 5,
}


def tokens(n: int) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "access_token": f"access-{n}",
            "token_type": "Bearer",
            "expires_in": 300,
            "refresh_token": f"ssc_rt.{ORG}.refresh-{n}",
        },
    )


def oauth(error: str) -> httpx2.Response:
    return httpx2.Response(400, json={"error": error})


def form(request: httpx2.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


AGENT_WHOAMI = {**WHOAMI, "is_agent": True, "client_id": "claude-code"}


def kept(n: int = 1, *, expires_in: int = 300, agent: str | None = None) -> Login:
    return Login(
        auth_url=AUTH,
        org_id=ORG,
        access_token=f"access-{n}",
        expires_at=int(time.time()) + expires_in,
        refresh_token=f"ssc_rt.{ORG}.refresh-{n}",
        agent=agent,
    )


def test_login_runs_the_device_flow_and_keeps_the_login(cli, fake_api, isolated):
    fake_api.add("POST", "/device/authorize", httpx2.Response(200, json=START))
    fake_api.add("POST", "/token", oauth("authorization_pending"), oauth("slow_down"), tokens(1))
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    r = cli("login", "--org", ORG, "--json", session=fake_api.session())
    assert r.code == 0, r.stderr
    assert LoginResult.model_validate(r.json()) == LoginResult(
        api_url=API, auth_url=AUTH, org_id=ORG, subject=WHOAMI["subject"], stored_in="keychain"
    )
    assert START["verification_uri_complete"] in r.stderr and "BCDF-GHJK" in r.stderr
    authorize, *polls, whoami = fake_api.seen
    assert str(authorize.url) == f"{AUTH}/device/authorize" and form(authorize) == {"org": ORG}
    assert [form(p)["device_code"] for p in polls] == [START["device_code"]] * 3
    assert whoami.headers["authorization"] == "Bearer access-1"
    login = read_login(API)
    assert login is not None and login.refresh_token == f"ssc_rt.{ORG}.refresh-1"
    assert "access-1" not in r.stdout + r.stderr and "refresh-1" not in r.stdout + r.stderr


@pytest.mark.parametrize(
    ("start", "polled", "detail"),
    [
        (oauth("invalid_request"), None, "cannot sign in"),
        (httpx2.Response(200, json=START), oauth("access_denied"), "refused"),
        (httpx2.Response(200, json=START), oauth("expired_token"), "expired"),
        (httpx2.Response(200, json=START), oauth("invalid_grant"), "expired"),
    ],
)
def test_a_login_that_does_not_finish_keeps_nothing(  # noqa: PLR0913
    cli, fake_api, isolated, start, polled, detail
):
    fake_api.add("POST", "/device/authorize", start)
    if polled is not None:
        fake_api.add("POST", "/token", polled)
    r = cli("login", "--org", ORG, "--json", session=fake_api.session())
    assert r.code == ExitCode.AUTH
    error = ErrorResult.model_validate(r.json()).error
    assert error.code == "LOGIN_FAILED" and detail in error.detail
    assert isolated.store == {}


def test_a_command_refreshes_a_login_about_to_end(cli, fake_api, isolated):
    store_login(API, kept(1, expires_in=30))
    fake_api.add("POST", "/token", tokens(2))
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    r = cli("whoami", "--json", session=fake_api.session())
    assert r.code == 0, r.stderr
    refresh, whoami = fake_api.seen
    assert form(refresh) == {
        "grant_type": "refresh_token",
        "refresh_token": f"ssc_rt.{ORG}.refresh-1",
    }
    assert whoami.headers["authorization"] == "Bearer access-2"
    login = read_login(API)
    assert login is not None and login.refresh_token == f"ssc_rt.{ORG}.refresh-2"


def test_a_live_login_is_used_as_it_is(cli, fake_api, isolated):
    store_login(API, kept(1))
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    assert cli("whoami", "--json", session=fake_api.session()).code == 0
    assert [r.url.path for r in fake_api.seen] == ["/v1/whoami"]


def test_a_login_another_process_refreshed_is_not_refreshed_again(fake_api, isolated):
    store_login(API, kept(1, expires_in=30))
    access = bearer(API, {}, transport=httpx2.MockTransport(fake_api.handler))
    assert callable(access)
    store_login(API, kept(2))
    assert access() == "access-2"
    assert fake_api.seen == []


def test_an_ended_login_is_forgotten(cli, fake_api, isolated):
    store_login(API, kept(1, expires_in=0))
    fake_api.add("POST", "/token", oauth("invalid_grant"))
    r = cli("whoami", "--json", session=fake_api.session())
    assert r.code == ExitCode.AUTH
    error = ErrorResult.model_validate(r.json()).error
    assert error.code == "LOGIN_ENDED" and f"ssc login --org {ORG}" in error.detail
    assert isolated.store == {}


def test_the_env_token_wins_over_a_login(fake_api, isolated):
    store_login(API, kept(1, expires_in=0))
    assert bearer(API, {"SSC_TOKEN": "t"}) == "t"


def test_logout_revokes_then_forgets(cli, fake_api, isolated):
    store_login(API, kept(1))
    fake_api.add("POST", "/revoke", httpx2.Response(200))
    r = cli("logout", "--json", session=fake_api.session())
    assert r.code == 0, r.stderr
    assert LogoutResult.model_validate(r.json()) == LogoutResult(
        api_url=API, revoked=True, cleared=True
    )
    assert form(fake_api.seen[0]) == {"token": f"ssc_rt.{ORG}.refresh-1"}
    assert isolated.store == {}


def test_logout_forgets_even_when_the_auth_host_is_down(cli, isolated):
    def down(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down")

    store_login(API, kept(1))
    s = Session(api_override=API, transport=httpx2.MockTransport(down))
    r = cli("logout", "--json", session=s)
    assert r.code == 0, r.stderr
    assert r.json() == {"api_url": API, "revoked": False, "cleared": True, "agent": False}
    assert "12 hours" in r.stderr


def test_logout_of_a_kept_token_only_forgets_it(cli, fake_api, isolated):
    isolated.store[(SERVICE, API)] = "plain-token"
    r = cli("logout", "--json", session=fake_api.session())
    assert r.json() == {"api_url": API, "revoked": False, "cleared": True, "agent": False}
    assert fake_api.seen == []


def test_an_agent_login_is_kept_apart_from_the_person_s(cli, fake_api, isolated):
    store_login(API, kept(7))
    fake_api.add("POST", "/device/authorize", httpx2.Response(200, json=START))
    fake_api.add("POST", "/token", tokens(1))
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=AGENT_WHOAMI))
    r = cli("login", "--org", ORG, "--agent", "claude-code", "--json", session=fake_api.session())
    assert r.code == 0, r.stderr
    assert r.json()["agent"] == "claude-code"
    authorize = fake_api.seen[0]
    assert form(authorize) == {"org": ORG, "agent": "claude-code"}
    mine, agents = read_login(API), read_login(API, agent=True)
    assert mine is not None and mine.access_token == "access-7" and mine.agent is None
    assert agents is not None and agents.access_token == "access-1"
    assert agents.agent == "claude-code"
    assert (SERVICE, entry(API, agent=True)) in isolated.store
    access = agent_bearer(API)
    assert callable(access) and access() == "access-1"
    assert bearer(API, {}) != access


def test_an_agent_login_the_api_does_not_see_as_the_agent_is_not_kept(cli, fake_api, isolated):
    fake_api.add("POST", "/device/authorize", httpx2.Response(200, json=START))
    fake_api.add("POST", "/token", tokens(1))
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    r = cli("login", "--org", ORG, "--agent", "claude-code", "--json", session=fake_api.session())
    assert r.code == ExitCode.AUTH
    assert ErrorResult.model_validate(r.json()).error.code == "LOGIN_FAILED"
    assert isolated.store == {}


def test_a_bad_agent_name_is_refused_before_anything_is_sent(cli, fake_api, isolated):
    r = cli("login", "--org", ORG, "--agent", "Claude Code", "--json", session=fake_api.session())
    assert r.code == ExitCode.AUTH
    assert "not an agent name" in ErrorResult.model_validate(r.json()).error.detail
    assert fake_api.seen == []


def test_an_ended_agent_login_says_how_to_sign_the_agent_in_again(cli, fake_api, isolated):
    store_login(API, kept(1, expires_in=0, agent="claude-code"))
    fake_api.add("POST", "/token", oauth("invalid_grant"))
    access = agent_bearer(API, transport=httpx2.MockTransport(fake_api.handler))
    assert callable(access)
    with pytest.raises(CliError) as err:
        access()
    assert f"ssc login --org {ORG} --agent claude-code" in err.value.body.detail
    assert isolated.store == {}


def test_logout_agent_ends_only_the_agent_s_login(cli, fake_api, isolated):
    store_login(API, kept(1))
    store_login(API, kept(2, agent="claude-code"))
    fake_api.add("POST", "/revoke", httpx2.Response(200))
    r = cli("logout", "--agent", "--json", session=fake_api.session())
    assert r.json() == {"api_url": API, "revoked": True, "cleared": True, "agent": True}
    assert form(fake_api.seen[0]) == {"token": f"ssc_rt.{ORG}.refresh-2"}
    assert read_login(API, agent=True) is None
    mine = read_login(API)
    assert mine is not None and mine.access_token == "access-1"


def test_the_auth_address():
    assert auth_url_for("https://api.delimitus.com", None, {}) == "https://auth.delimitus.com"
    assert auth_url_for("https://api.test:8443", None, {}) == "https://auth.test:8443"
    assert auth_url_for(API, None, {"SSC_AUTH_URL": "http://127.0.0.1:9/"}) == "http://127.0.0.1:9"
    assert auth_url_for(API, "https://sso.test", {"SSC_AUTH_URL": "x"}) == "https://sso.test"
    for api, override in (("http://127.0.0.1:8000", None), (API, "http://auth.test")):
        with pytest.raises(CliError) as err:
            auth_url_for(api, override, {})
        assert err.value.exit_code == ExitCode.USAGE
