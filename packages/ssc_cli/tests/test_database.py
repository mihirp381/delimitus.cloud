"""``ssc database rotate``: a new database password, put live by a deployment; the password is
never shown."""

import httpx2
import pytest

from ssc_cli.credentials import SERVICE
from ssc_cli.errors import ExitCode
from ssc_cli.shapes import DatabaseRotateResult

USR = "usr_aaaaaaaaaaaaaaaaaaaa"
APP_ID = "app_aaaaaaaaaaaaaaaaaaaa"
PREVIEW = "env_prevprevprevprevprev"
PROD = "env_prodprodprodprodprod"
DEP = "dep_aaaaaaaaaaaaaaaaaaaa"
ROTATE = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/database/rotate"
ROTATED_AT = "2026-10-05T10:00:00Z"
IN_FLIGHT_FIX = (
    "Another deployment is running in preview. Wait for it (`ssc status demo`), then run this "
    "again."
)


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


def _rotated(operation_id: str | None = DEP) -> httpx2.Response:
    body = {"environment_id": PREVIEW, "rotated_at": ROTATED_AT, "operation_id": operation_id}
    if operation_id is None:
        return httpx2.Response(200, json=body)
    return httpx2.Response(202, json=body, headers={"Location": f"/v1/operations/{operation_id}"})


def _operation(state: str) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "operation_id": DEP,
            "kind": "deploy",
            "state": state,
            "app_id": APP_ID,
            "environment_id": PREVIEW,
            "release_id": "rel_aaaaaaaaaaaaaaaaaaaa",
            "started_at": "2026-10-05T10:00:00Z",
            "finished_at": None if state == "running" else "2026-10-05T10:01:00Z",
        },
    )


@pytest.fixture
def api(fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    fake_api.add("GET", f"/v1/apps/{APP_ID}", httpx2.Response(200, json=_app()))
    return fake_api


def _rotate(cli, api, *extra: str, env: str = "preview"):
    return cli("database", "rotate", APP_ID, "--env", env, *extra, session=api.session())


def _posts(api):
    return [q for q in api.seen if q.method == "POST"]


def test_rotate_started_says_which_deployment_puts_it_live(cli, api):
    api.add("POST", ROTATE, _rotated())
    r = _rotate(cli, api)
    assert r.code == 0, r.stderr
    assert r.stdout == (
        f"Rotated the preview database password of demo; deployment {DEP} puts it live.\n"
        "Follow it with `ssc status demo`.\n"
    )
    (post,) = _posts(api)
    assert post.url.path == ROTATE
    assert post.content == b""
    assert not [q for q in api.seen if q.url.path.startswith("/v1/operations")]


def test_rotate_with_wait_ends_when_the_deployment_is_healthy(cli, api):
    api.add("POST", ROTATE, _rotated())
    api.add("GET", f"/v1/operations/{DEP}", _operation("running"), _operation("healthy"))
    r = _rotate(cli, api, "--wait")
    assert r.code == 0, r.stderr
    assert r.stdout == "Rotated the preview database password of demo; it is live.\n"


def test_rotate_with_nothing_live_says_the_next_deployment_takes_it(cli, api):
    api.add("POST", ROTATE, _rotated(None))
    r = _rotate(cli, api, "--wait")
    assert r.code == 0, r.stderr
    assert r.stdout == (
        "Rotated the preview database password of demo. Nothing runs there, so its next "
        "deployment takes the new password.\n"
    )
    assert not [q for q in api.seen if q.url.path.startswith("/v1/operations")]


@pytest.mark.parametrize(
    ("operation_id", "wait", "state"),
    [(DEP, False, "pending"), (DEP, True, "healthy"), (None, False, None)],
)
def test_rotate_json(cli, api, operation_id, wait, state):
    api.add("POST", ROTATE, _rotated(operation_id))
    api.add("GET", f"/v1/operations/{DEP}", _operation("healthy"))
    r = _rotate(cli, api, "--json", *(["--wait"] if wait else []))
    assert r.code == 0, r.stderr
    assert DatabaseRotateResult.model_validate(r.json()) == DatabaseRotateResult(
        app_id=APP_ID,
        slug="demo",
        environment="preview",
        environment_id=PREVIEW,
        rotated_at=ROTATED_AT,
        operation_id=operation_id,
        state=state,
    )


def test_env_is_required(cli, api):
    r = cli("database", "rotate", APP_ID, session=api.session())
    assert r.code == ExitCode.USAGE
    assert api.seen == []
    bad = _rotate(cli, api, env="staging")
    assert bad.code == ExitCode.USAGE
    assert api.seen == []


def test_in_flight_refusal_says_to_wait(cli, api, fake_problem):
    api.add("POST", ROTATE, fake_problem(409, "DEPLOYMENT_IN_FLIGHT"))
    r = _rotate(cli, api)
    assert r.code == ExitCode.FAILED
    assert r.stdout == ""
    assert "Code: DEPLOYMENT_IN_FLIGHT" in r.stderr
    assert f"Fix: {IN_FLIGHT_FIX}" in r.stderr
    machine = _rotate(cli, api, "--json")
    assert machine.code == ExitCode.FAILED
    assert machine.json()["error"]["code"] == "DEPLOYMENT_IN_FLIGHT"


def test_other_refusals_keep_the_apis_text(cli, api, fake_problem):
    api.add("POST", ROTATE, fake_problem(404, "NOT_FOUND"))
    r = _rotate(cli, api)
    assert r.code == ExitCode.FAILED
    assert "Error: title for NOT_FOUND" in r.stderr
    assert "Fix:" not in r.stderr


def test_the_post_carries_an_idempotency_key_reused_on_retry(cli, api):
    api.add("POST", ROTATE, httpx2.Response(500, json={}), _rotated())
    r = _rotate(cli, api)
    assert r.code == 0, r.stderr
    first, second = _posts(api)
    assert first.headers["idempotency-key"]
    assert first.headers["idempotency-key"] == second.headers["idempotency-key"]
