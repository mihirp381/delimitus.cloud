"""The HTTP client: retries, idempotency keys, Retry-After, refusals and network failures."""

import uuid

import httpx2
import pytest

from ssc_cli import __version__
from ssc_cli.api import BACKOFF, MAX_RETRY_AFTER, ApiClient
from ssc_cli.errors import CliError, ExitCode
from ssc_cli.models import GrantIn

WHOAMI = {
    "org_id": "org_aaaaaaaaaaaaaaaaaaaa",
    "subject": "usr_aaaaaaaaaaaaaaaaaaaa",
    "kind": "user",
    "credential_id": "cred_1",
    "is_agent": False,
    "client_id": None,
}
APP = {
    "id": "app_aaaaaaaaaaaaaaaaaaaa",
    "slug": "demo",
    "owner_user_id": "usr_aaaaaaaaaaaaaaaaaaaa",
    "status": "active",
    "created_at": "2026-09-29T00:00:00Z",
    "environments": [],
}


def client(fake_api, sleeps: list[float] | None = None) -> ApiClient:
    record = sleeps.append if sleeps is not None else (lambda _: None)
    return ApiClient(
        "https://api.test", "tok", transport=httpx2.MockTransport(fake_api.handler), sleep=record
    )


def test_sends_token_and_user_agent(fake_api):
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    with client(fake_api) as c:
        assert c.whoami().subject == WHOAMI["subject"]
    req = fake_api.seen[0]
    assert req.headers["authorization"] == "Bearer tok"
    assert req.headers["user-agent"] == f"ssc-cli/{__version__}"


def test_tolerates_fields_it_does_not_know(fake_api):
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json={**WHOAMI, "new_field": 1}))
    with client(fake_api) as c:
        assert c.whoami().org_id == WHOAMI["org_id"]


def test_get_retries_5xx_with_backoff(fake_api, fake_problem):
    fake_api.add(
        "GET",
        "/v1/whoami",
        fake_problem(503, "UNAVAILABLE"),
        fake_problem(502, "UNAVAILABLE"),
        httpx2.Response(200, json=WHOAMI),
    )
    sleeps: list[float] = []
    with client(fake_api, sleeps) as c:
        c.whoami()
    assert sleeps == list(BACKOFF[:2])


def test_get_gives_up_after_three_retries(fake_api, fake_problem):
    fake_api.add("GET", "/v1/whoami", fake_problem(503, "UNAVAILABLE"))
    sleeps: list[float] = []
    with client(fake_api, sleeps) as c, pytest.raises(CliError) as err:
        c.whoami()
    assert len(fake_api.seen) == 4
    assert sleeps == list(BACKOFF)
    assert err.value.body.status == 503
    assert err.value.exit_code == ExitCode.FAILED


def test_post_reuses_one_idempotency_key_across_retries(fake_api, fake_problem):
    fake_api.add(
        "POST",
        "/v1/apps",
        fake_problem(502, "UNAVAILABLE"),
        fake_problem(409, "IDEMPOTENCY_IN_FLIGHT"),
        httpx2.Response(201, json=APP),
    )
    with client(fake_api) as c:
        assert c.create_app("demo").slug == "demo"
    keys = {r.headers["idempotency-key"] for r in fake_api.seen}
    assert len(fake_api.seen) == 3
    assert len(keys) == 1
    uuid.UUID(keys.pop())


def test_each_post_gets_its_own_key(fake_api):
    fake_api.add("POST", "/v1/apps", httpx2.Response(201, json=APP))
    with client(fake_api) as c:
        c.create_app("demo")
        c.create_app("demo")
    assert (
        fake_api.seen[0].headers["idempotency-key"] != fake_api.seen[1].headers["idempotency-key"]
    )


def test_conflict_is_not_retried(fake_api, fake_problem):
    fake_api.add("POST", "/v1/apps", fake_problem(409, "ALREADY_EXISTS"))
    with client(fake_api) as c, pytest.raises(CliError) as err:
        c.create_app("demo")
    assert len(fake_api.seen) == 1
    assert err.value.body.code == "ALREADY_EXISTS"
    assert err.value.exit_code == ExitCode.FAILED


def test_put_is_never_retried_on_5xx(fake_api, fake_problem):
    path = "/v1/apps/app_x/environments/env_y/grants"
    fake_api.add("PUT", path, fake_problem(503, "UNAVAILABLE"))
    with client(fake_api) as c, pytest.raises(CliError):
        c.put_grants("app_x", "env_y", [GrantIn(role="user", subject_kind="org")], '"1"')
    assert len(fake_api.seen) == 1
    assert fake_api.seen[0].headers["if-match"] == '"1"'


def test_429_waits_for_retry_after_once(fake_api, fake_problem):
    fake_api.add(
        "GET",
        "/v1/whoami",
        fake_problem(429, "RATE_LIMITED", **{"retry-after": "7"}),
        httpx2.Response(200, json=WHOAMI),
    )
    sleeps: list[float] = []
    with client(fake_api, sleeps) as c:
        c.whoami()
    assert sleeps == [7.0]


def test_429_twice_is_a_refusal_and_the_wait_is_capped(fake_api, fake_problem):
    fake_api.add(
        "PUT",
        "/v1/apps/a/environments/e/grants",
        fake_problem(429, "RATE_LIMITED", **{"retry-after": "3600"}),
    )
    sleeps: list[float] = []
    with client(fake_api, sleeps) as c, pytest.raises(CliError) as err:
        c.put_grants("a", "e", [], '"1"')
    assert sleeps == [MAX_RETRY_AFTER]
    assert len(fake_api.seen) == 2
    assert err.value.body.code == "RATE_LIMITED"


def test_connection_errors_retry_then_exit_5(fake_api):
    def refuse(request: httpx2.Request) -> httpx2.Response:
        fake_api.seen.append(request)
        raise httpx2.ConnectError("refused", request=request)

    c = ApiClient(
        "https://api.test", "tok", transport=httpx2.MockTransport(refuse), sleep=lambda _: None
    )
    with c, pytest.raises(CliError) as err:
        c.list_apps()
    assert len(fake_api.seen) == 4
    assert err.value.exit_code == ExitCode.NETWORK
    assert err.value.body.code == "NETWORK_ERROR"
    assert err.value.body.status is None


def test_401_exits_3(fake_api, fake_problem):
    fake_api.add("GET", "/v1/whoami", fake_problem(401, "UNAUTHENTICATED"))
    with client(fake_api) as c, pytest.raises(CliError) as err:
        c.whoami()
    assert err.value.exit_code == ExitCode.AUTH
    assert err.value.body.request_id == "req_test"


def test_error_without_problem_body(fake_api):
    fake_api.add(
        "PUT",
        "/v1/apps/a/environments/e/grants",
        httpx2.Response(500, text="<html>oops</html>", headers={"x-request-id": "req_9"}),
    )
    with client(fake_api) as c, pytest.raises(CliError) as err:
        c.put_grants("a", "e", [], '"1"')
    assert err.value.body.code == "BAD_RESPONSE"
    assert err.value.body.status == 500
    assert err.value.body.request_id == "req_9"


def test_success_in_an_unknown_shape(fake_api):
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json={"hello": "world"}))
    with client(fake_api) as c, pytest.raises(CliError) as err:
        c.whoami()
    assert err.value.body.code == "BAD_RESPONSE"
    assert err.value.exit_code == ExitCode.FAILED


def test_path_segments_are_quoted(fake_api):
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    with client(fake_api) as c, pytest.raises(CliError):
        c.get_app("app_x/../../whoami")
    assert fake_api.seen[0].url.raw_path == b"/v1/apps/app_x%2F..%2F..%2Fwhoami"


def test_grants_etag_falls_back_to_the_body_version(fake_api):
    body = {"environment_id": "env_1", "grants_version": 4, "grants": []}
    fake_api.add("GET", "/v1/apps/a/environments/e/grants", httpx2.Response(200, json=body))
    with client(fake_api) as c:
        _, etag = c.get_grants("a", "e")
    assert etag == '"4"'
