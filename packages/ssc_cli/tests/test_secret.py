"""``ssc secret`` (SSC-026): the value goes from stdin to the cell's secret intake, never to the
API, and there is no ``secret get``. The value here is fake."""

import json

import httpx2
import pytest

from ssc_cli.credentials import SERVICE
from ssc_cli.errors import ExitCode
from ssc_cli.shapes import SecretSetResult, SecretsResult

USR = "usr_aaaaaaaaaaaaaaaaaaaa"
APP_ID = "app_aaaaaaaaaaaaaaaaaaaa"
PREVIEW = "env_prevprevprevprevprev"
PROD = "env_prodprodprodprodprod"
DEP = "dep_aaaaaaaaaaaaaaaaaaaa"
SID = "ssc-a-prevprevprevprevprev-STRIPE_KEY"
VALUE = "fake-secret-value-for-cli-tests"
GRANT = "grant-token-not-real"
INTAKE = f"https://ssc--secrets.cell.test/v1/secrets/{SID}"
SECRETS = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/secrets"


def _app() -> dict[str, object]:
    envs = [
        {"id": PREVIEW, "name": "preview", "config_version": 1, "grants_version": 1},
        {"id": PROD, "name": "prod", "config_version": 1, "grants_version": 1},
    ]
    return {
        "id": APP_ID,
        "slug": "demo",
        "owner_user_id": USR,
        "status": "active",
        "created_at": "2026-09-29T00:00:00Z",
        "environments": envs,
    }


def _granted() -> httpx2.Response:
    upload = {
        "method": "PUT",
        "url": f"{INTAKE}?grant={'n' * 32}",
        "headers": {
            "Authorization": f"Bearer {GRANT}",
            "Content-Type": "application/octet-stream",
        },
        "expires_at": "2026-10-03T00:10:00Z",
    }
    return httpx2.Response(201, json={"name": "STRIPE_KEY", "upload": upload})


def _recorded(operation_id: str | None = DEP, *, changed: bool = True) -> httpx2.Response:
    body = {"name": "STRIPE_KEY", "version": "3", "changed": changed, "operation_id": operation_id}
    return httpx2.Response(202 if operation_id else 200, json=body)


@pytest.fixture
def api(fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    fake_api.add("GET", f"/v1/apps/{APP_ID}", httpx2.Response(200, json=_app()))
    fake_api.add("POST", f"{SECRETS}/STRIPE_KEY/grants", _granted())
    fake_api.add(
        "PUT", f"/v1/secrets/{SID}", httpx2.Response(201, json={"secret": SID, "version": "3"})
    )
    return fake_api


def _set(cli, api, *extra: str, value: str = VALUE + "\n"):
    args = ("secret", "set", APP_ID, "STRIPE_KEY", "--env", "preview", *extra)
    return cli(*args, input=value, session=api.session())


def test_set_sends_the_value_only_to_the_intake(cli, api):
    api.add("PUT", f"{SECRETS}/STRIPE_KEY", _recorded())
    r = _set(cli, api, "--json")
    assert r.code == 0, (r.stdout, r.stderr)
    out = SecretSetResult.model_validate(r.json())
    assert (out.name, out.version, out.changed, out.operation_id, out.state) == (
        "STRIPE_KEY",
        "3",
        True,
        DEP,
        "pending",
    )
    (intake,) = [q for q in api.seen if q.url.host == "ssc--secrets.cell.test"]
    assert intake.content == VALUE.encode()
    assert intake.headers["Authorization"] == f"Bearer {GRANT}"
    to_api = [q for q in api.seen if q.url.host == "api.test"]
    assert {(q.method, q.url.path) for q in to_api} == {
        ("GET", f"/v1/apps/{APP_ID}"),
        ("POST", f"{SECRETS}/STRIPE_KEY/grants"),
        ("PUT", f"{SECRETS}/STRIPE_KEY"),
    }
    assert not [q for q in to_api if VALUE.encode() in q.content]
    (put,) = [q for q in to_api if q.method == "PUT"]
    assert json.loads(put.content) == {"version": "3"}
    assert VALUE not in r.stdout + r.stderr
    assert GRANT not in r.stdout + r.stderr


def test_set_waits_for_the_deployment(cli, api):
    api.add("PUT", f"{SECRETS}/STRIPE_KEY", _recorded())
    operation = {
        "operation_id": DEP,
        "kind": "deploy",
        "state": "healthy",
        "app_id": APP_ID,
        "environment_id": PREVIEW,
        "release_id": "rel_aaaaaaaaaaaaaaaaaaaa",
        "started_at": "2026-09-29T00:00:00Z",
        "finished_at": "2026-09-29T00:01:00Z",
    }
    api.add("GET", f"/v1/operations/{DEP}", httpx2.Response(200, json=operation))
    r = _set(cli, api, "--wait")
    assert r.code == 0, r.stderr
    assert r.stdout == "STRIPE_KEY version 3 is live in preview of demo.\n"


def test_set_before_any_deployment_says_the_next_one_takes_it(cli, api):
    api.add("PUT", f"{SECRETS}/STRIPE_KEY", _recorded(None))
    r = _set(cli, api)
    assert r.code == 0, r.stderr
    assert "The next deployment of that environment puts it live." in r.stdout


def test_a_value_keeps_inner_spaces_and_loses_one_line_ending(cli, api):
    api.add("PUT", f"{SECRETS}/STRIPE_KEY", _recorded())
    assert _set(cli, api, value="  two words \r\n").code == 0
    (intake,) = [q for q in api.seen if q.method == "PUT" and q.url.host != "api.test"]
    assert intake.content == b"  two words "


@pytest.mark.parametrize("value", ["", "\n", "x" * (64 * 1024 + 1)])
def test_an_empty_or_huge_value_is_refused_before_any_request(cli, api, value):
    r = _set(cli, api, "--json", value=value)
    assert r.code == ExitCode.USAGE
    assert json.loads(r.stdout)["error"]["code"] == "BAD_SECRET_INPUT"
    assert api.seen == []


@pytest.mark.parametrize("name", ["stripe_key", "PORT", "SSC_TOKEN", "K_SERVICE"])
def test_a_platform_or_lowercase_name_is_a_usage_error(cli, api, name):
    r = cli("secret", "set", APP_ID, name, "--env", "preview", input=VALUE, session=api.session())
    assert r.code == ExitCode.USAGE
    assert api.seen == []


def test_the_value_is_never_an_argument(cli, api):
    r = cli("secret", "set", APP_ID, "STRIPE_KEY", VALUE, "--env", "preview", session=api.session())
    assert r.code == ExitCode.USAGE
    assert api.seen == []


def test_a_refused_grant_names_no_value_or_grant(cli, api):
    api.routes[("PUT", f"/v1/secrets/{SID}")] = [
        httpx2.Response(403, json={"code": "GRANT_REFUSED", "message": "refused"})
    ]
    r = _set(cli, api, "--json")
    assert r.code == ExitCode.FAILED
    error = json.loads(r.stdout)["error"]
    assert error["code"] == "UPLOAD_FAILED"
    assert "HTTP 403 GRANT_REFUSED" in error["detail"]
    for leaked in (VALUE, GRANT, "grant=", "ssc--secrets"):
        assert leaked not in r.stdout + r.stderr
    assert not [q for q in api.seen if q.method == "PUT" and q.url.host == "api.test"]


def test_an_agent_credential_is_told_a_person_sets_secrets(cli, api, fake_problem):
    api.routes[("POST", f"{SECRETS}/STRIPE_KEY/grants")] = [
        fake_problem(403, "AGENT_SESSION_REFUSED")
    ]
    r = _set(cli, api)
    assert r.code == ExitCode.FAILED
    assert "never an agent" in r.stderr
    assert not [q for q in api.seen if q.url.host != "api.test"]


def test_list_shows_names_and_versions(cli, api):
    body = {
        "environment_id": PREVIEW,
        "items": [
            {
                "name": "STRIPE_KEY",
                "version": "3",
                "live_version": "2",
                "updated_at": "2026-10-03T00:00:00Z",
            }
        ],
    }
    api.add("GET", SECRETS, httpx2.Response(200, json=body))
    r = cli("secret", "list", APP_ID, "--env", "preview", "--json", session=api.session())
    assert r.code == 0, r.stderr
    out = SecretsResult.model_validate(r.json())
    assert [(s.name, s.version, s.live_version) for s in out.secrets] == [("STRIPE_KEY", "3", "2")]
    human = cli("secret", "list", APP_ID, "--env", "preview", session=api.session())
    assert "STRIPE_KEY" in human.stdout


@pytest.mark.parametrize("verb", ["get", "show", "read", "reveal", "export"])
def test_there_is_no_secret_get(cli, api, verb):
    r = cli("secret", verb, APP_ID, "STRIPE_KEY", "--env", "preview", session=api.session())
    assert r.code == ExitCode.USAGE
    assert api.seen == []
