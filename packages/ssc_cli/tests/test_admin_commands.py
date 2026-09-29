"""``ssc disable``, ``ssc enable``, ``ssc access explain`` and ``ssc apps --mine``."""

import json
import time
import uuid

import httpx2
import pytest

from ssc_cli.credentials import SERVICE
from ssc_cli.errors import ExitCode
from ssc_cli.session import Session
from ssc_cli.shapes import AccessResult, AppResult, AppsResult, DisableResult

APP_ID = "app_aaaaaaaaaaaaaaaaaaaa"
USR = "usr_aaaaaaaaaaaaaaaaaaaa"
OTHER = "usr_bbbbbbbbbbbbbbbbbbbb"
GRP = "grp_aaaaaaaaaaaaaaaaaaaa"
PROD = "env_prodprodprodprodprod"
PREVIEW = "env_prevprevprevprevprev"
RUN = "kil_aaaaaaaaaaaaaaaaaaaa"
KILL = f"/v1/apps/{APP_ID}/kill-switch"
AT = "2026-09-29T00:00:00Z"


def _app(status: str = "active") -> dict[str, object]:
    return {
        "id": APP_ID,
        "slug": "demo",
        "owner_user_id": USR,
        "status": status,
        "created_at": AT,
        "environments": [
            {"id": PREVIEW, "name": "preview", "config_version": 1, "grants_version": 1},
            {"id": PROD, "name": "prod", "config_version": 1, "grants_version": 1},
        ],
    }


@pytest.fixture
def scripted(fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    summary = {k: _app()[k] for k in ("id", "slug", "owner_user_id", "status")}
    fake_api.add("GET", "/v1/apps", httpx2.Response(200, json={"apps": [summary]}))
    fake_api.add("GET", f"/v1/apps/{APP_ID}", httpx2.Response(200, json=_app()))
    return fake_api


def _step(name: str, state: str = "done", error: str | None = None) -> dict[str, object]:
    return {
        "name": name,
        "state": state,
        "snapshot_version": None,
        "started_at": AT,
        "finished_at": None if state == "running" else AT,
        "elapsed_ms": None if state == "running" else 12,
        "attempts": 1,
        "error": error,
    }


def _run(state: str, *steps: dict[str, object], mode: str = "disable") -> httpx2.Response:
    body = {
        "run_id": RUN,
        "app_id": APP_ID,
        "mode": mode,
        "state": state,
        "steps": list(steps),
        "started_at": AT,
        "finished_at": None if state == "running" else AT,
        "total_ms": None if state == "running" else 40,
    }
    return httpx2.Response(200, json=body)


def _accepted() -> httpx2.Response:
    return httpx2.Response(202, json={"run_id": RUN, "state": "running"})


def _fix(stderr: str) -> str:
    (fix,) = [line for line in stderr.splitlines() if line.startswith("Fix: ")]
    return fix.removeprefix("Fix: ")


# ── disable ─────────────────────────────────────────────────────────────────


def test_disable_pulls_the_switch_and_follows_the_run(cli, scripted):
    scripted.add("POST", KILL, _accepted())
    scripted.add(
        "GET",
        f"{KILL}/{RUN}",
        _run("running", _step("gateway_deny", "running")),
        _run("completed", _step("gateway_deny"), _step("scale_to_zero")),
    )
    r = cli("disable", "demo", "--json", session=scripted.session())
    assert r.code == 0, r.stdout
    result = DisableResult.model_validate(r.json())
    assert (result.mode, result.status, result.state, result.run_id) == (
        "disable",
        "disabled",
        "completed",
        RUN,
    )
    assert [s.name for s in result.steps] == ["gateway_deny", "scale_to_zero"]
    (post,) = [q for q in scripted.seen if q.method == "POST"]
    assert json.loads(post.content) == {"mode": "disable"}
    assert post.headers["idempotency-key"]
    assert len([q for q in scripted.seen if q.url.path == f"{KILL}/{RUN}"]) == 2


def test_quarantine_sends_its_mode_and_the_human_output_says_how_to_undo(cli, scripted):
    scripted.add("POST", KILL, _accepted())
    scripted.add(
        "GET", f"{KILL}/{RUN}", _run("completed", _step("gateway_deny"), mode="quarantine")
    )
    r = cli("disable", "demo", "--quarantine", session=scripted.session())
    assert r.code == 0, r.stderr
    (post,) = [q for q in scripted.seen if q.method == "POST"]
    assert json.loads(post.content) == {"mode": "quarantine"}
    assert r.stdout.startswith("demo is quarantined and denies every request.\n")
    assert "Undo with `ssc enable demo`." in r.stdout


def test_a_failed_step_exits_1_and_says_the_app_stays_stopped(cli, scripted):
    scripted.add("POST", KILL, _accepted())
    failed = _run("failed", _step("gateway_deny"), _step("scale_to_zero", "failed", "NOT_STOPPED"))
    scripted.add("GET", f"{KILL}/{RUN}", failed)
    r = cli("disable", "demo", "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    error = r.json()["error"]
    assert (error["code"], error["status"]) == ("KILL_SWITCH_FAILED", None)
    assert "stays stopped" in error["detail"]
    assert "scale_to_zero (NOT_STOPPED)" in error["detail"]
    assert error["instance"] == f"{KILL}/{RUN}"


def test_following_the_run_gives_up_after_the_timeout(cli, scripted):
    scripted.add("POST", KILL, _accepted())
    scripted.add("GET", f"{KILL}/{RUN}", _run("running", _step("gateway_deny", "running")))
    r = cli("disable", "demo", "--timeout", "4", "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    error = r.json()["error"]
    assert error["code"] == "WAIT_TIMED_OUT"
    assert "stopped already" in error["detail"]
    assert len([q for q in scripted.seen if q.url.path == f"{KILL}/{RUN}"]) == 3


@pytest.mark.parametrize(
    ("status", "code", "fix"),
    [
        (403, "FORBIDDEN", "only an active org admin can stop or enable an app"),
        (409, "APP_NOT_ACTIVE", "demo is stopped already"),
        (409, "KILL_SWITCH_IN_FLIGHT", "an earlier pull of the kill switch is still running"),
    ],
)
def test_disable_refusals_say_what_to_do(cli, scripted, fake_problem, status, code, fix):
    scripted.add("POST", KILL, fake_problem(status, code))
    r = cli("disable", "demo", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert f"Code: {code}" in r.stderr
    assert fix in _fix(r.stderr)


def test_quarantining_a_quarantined_app_says_so(cli, scripted, fake_problem):
    scripted.add("POST", KILL, fake_problem(409, "APP_NOT_ACTIVE"))
    r = cli("disable", "demo", "--quarantine", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert "demo is already quarantined" in _fix(r.stderr)


def test_losing_the_run_still_says_the_app_is_stopped(cli, scripted, fake_problem):
    scripted.add("POST", KILL, _accepted())
    scripted.add("GET", f"{KILL}/{RUN}", fake_problem(401, "UNAUTHENTICATED"))
    r = cli("disable", "demo", "--json", session=scripted.session())
    assert r.code != 0
    error = r.json()["error"]
    assert error["code"] == "UNAUTHENTICATED"
    assert f"stopped already and denies every request; kill switch run {RUN}" in error["detail"]
    assert error["instance"]


@pytest.mark.parametrize(
    ("status", "fix", "absent"),
    [
        ("disabled", "`ssc disable demo --quarantine` quarantines it too", "ssc enable"),
        ("quarantined", "demo is already quarantined; `ssc enable demo` undoes it", "--quarantine"),
    ],
)
def test_disabling_a_stopped_app_says_what_it_is(cli, scripted, fake_problem, status, fix, absent):
    stopped = _app(status)
    summary = {k: stopped[k] for k in ("id", "slug", "owner_user_id", "status")}
    scripted.routes[("GET", "/v1/apps")] = [httpx2.Response(200, json={"apps": [summary]})]
    scripted.routes[("GET", f"/v1/apps/{APP_ID}")] = [httpx2.Response(200, json=stopped)]
    scripted.add("POST", KILL, fake_problem(409, "APP_NOT_ACTIVE"))
    r = cli("disable", "demo", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert fix in _fix(r.stderr)
    assert absent not in _fix(r.stderr)


# ── enable ──────────────────────────────────────────────────────────────────


def test_enable_answers_the_app_active_again(cli, scripted):
    scripted.add("POST", f"/v1/apps/{APP_ID}/enable", httpx2.Response(200, json=_app()))
    r = cli("enable", "demo", "--json", session=scripted.session())
    assert r.code == 0, r.stdout
    result = AppResult.model_validate(r.json())
    assert (result.id, result.status) == (APP_ID, "active")
    (post,) = [q for q in scripted.seen if q.method == "POST"]
    assert post.headers["idempotency-key"]
    assert post.content == b""


@pytest.mark.parametrize(
    ("status", "code", "fix"),
    [
        (403, "FORBIDDEN", "only an active org admin can stop or enable an app"),
        (409, "APP_ALREADY_ACTIVE", "demo is active already"),
        (409, "KILL_SWITCH_IN_FLIGHT", "the kill switch is still stopping demo"),
    ],
)
def test_enable_refusals_say_what_to_do(cli, scripted, fake_problem, status, code, fix):
    scripted.add("POST", f"/v1/apps/{APP_ID}/enable", fake_problem(status, code))
    r = cli("enable", "demo", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert fix in _fix(r.stderr)


# ── access explain ──────────────────────────────────────────────────────────


def _explained(**changes: object) -> httpx2.Response:
    body = {
        "user_id": OTHER,
        "environment_id": PROD,
        "allowed": True,
        "role": "user",
        "floor": "user",
        "reason": "granted",
        "grants": [
            {
                "grant_id": "gnt_aaaaaaaaaaaaaaaaaaaa",
                "role": "user",
                "subject_kind": "group",
                "subject_id": GRP,
                "group_name": "Finance",
            }
        ],
        "evaluated_from": "live",
        "published_version": 7,
    } | changes
    return httpx2.Response(200, json=body)


ACCESS = f"/v1/apps/{APP_ID}/environments/{PROD}/access"


def test_access_explain_names_the_grant_and_its_group(cli, scripted):
    scripted.add("GET", ACCESS, _explained())
    r = cli("access", "explain", "demo", OTHER, session=scripted.session())
    assert r.code == 0, r.stderr
    assert r.stdout.startswith(f"{OTHER} can open prod of demo as user, through the grants below.")
    assert "gnt_aaaaaaaaaaaaaaaaaaaa" in r.stdout
    assert "Finance" in r.stdout
    assert "newest published: 7" in r.stdout
    (get,) = [q for q in scripted.seen if q.url.path == ACCESS]
    assert get.url.params["user_id"] == OTHER


def test_access_explain_defaults_to_the_caller(cli, scripted):
    scripted.add(
        "GET",
        ACCESS,
        _explained(user_id=USR, allowed=False, role=None, reason="no_grant", grants=[]),
    )
    r = cli("access", "explain", "demo", "--json", session=scripted.session())
    assert r.code == 0, r.stdout
    result = AccessResult.model_validate(r.json())
    assert (result.user_id, result.allowed, result.reason, result.grants) == (
        USR,
        False,
        "no_grant",
        [],
    )
    (get,) = [q for q in scripted.seen if q.url.path == ACCESS]
    assert "user_id" not in get.url.params


def test_access_explain_looks_an_email_up_and_checks_preview(cli, scripted):
    person = {
        "id": OTHER,
        "display_name": "Ann",
        "email": "ann@example.com",
        "role": "member",
        "status": "active",
    }
    scripted.add("GET", "/v1/users", httpx2.Response(200, json={"users": [person]}))
    path = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/access"
    below = [
        {
            "grant_id": "gnt_bbbbbbbbbbbbbbbbbbbb",
            "role": "user",
            "subject_kind": "org",
            "subject_id": None,
            "group_name": None,
        }
    ]
    scripted.add(
        "GET",
        path,
        _explained(
            environment_id=PREVIEW,
            allowed=False,
            role=None,
            floor="builder",
            reason="below_floor",
            grants=below,
        ),
    )
    r = cli(
        "access",
        "explain",
        "demo",
        "ann@example.com",
        "--env",
        "preview",
        session=scripted.session(),
    )
    assert r.code == 0, r.stderr
    assert "preview takes builder grants or higher" in r.stdout
    (get,) = [q for q in scripted.seen if q.url.path == path]
    assert get.url.params["user_id"] == OTHER


def test_access_explain_refused_says_who_may_ask(cli, scripted, fake_problem):
    scripted.add("GET", ACCESS, fake_problem(403, "FORBIDDEN"))
    r = cli("access", "explain", "demo", OTHER, session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert "a builder on prod can see who can open prod" in _fix(r.stderr)


@pytest.mark.parametrize("who", [GRP, "Finance", "usr_short"])
def test_access_explain_takes_only_a_person(cli, fake_api, who):
    r = cli("access", "explain", "demo", who, session=fake_api.session())
    assert r.code == ExitCode.USAGE
    assert fake_api.seen == []


# ── apps --mine ─────────────────────────────────────────────────────────────


def test_apps_mine_asks_for_the_callers_apps(cli, scripted):
    scripted.routes[("GET", "/v1/apps")] = [httpx2.Response(200, json={"apps": []})]
    r = cli("apps", "--mine", session=scripted.session())
    assert r.code == 0, r.stderr
    assert r.stdout.startswith("You cannot deploy to any app yet.")
    (get,) = scripted.seen
    assert get.url.params["builder"] == "me"
    plain = cli("apps", "--json", session=scripted.session())
    assert "builder" not in scripted.seen[-1].url.params
    AppsResult.model_validate(plain.json())


# ── on the dev stack ────────────────────────────────────────────────────────


def _short_sleep(_: float) -> None:
    time.sleep(0.05)


@pytest.fixture
def on_live(cli, live, isolated):
    isolated.set_password(SERVICE, live.url, live.token())

    def run(*args: str):
        return cli(*args, session=Session(api_override=live.url, sleep=_short_sleep))

    return run


def test_live_disable_then_enable(on_live, live):
    name = f"t{uuid.uuid4().hex[:12]}"
    assert on_live("apps", "create", name).code == 0
    off = on_live("disable", name, "--timeout", "3600", "--json")
    assert off.code == 0, (off.stdout, off.stderr)
    result = DisableResult.model_validate(off.json())
    assert (result.status, result.state) == ("disabled", "completed")
    assert on_live("status", name, "--json").json()["status"] == "disabled"
    again = on_live("disable", name, "--json")
    assert again.json()["error"]["code"] == "APP_NOT_ACTIVE"
    on = on_live("enable", name, "--json")
    assert on.code == 0, on.stdout
    assert on.json()["status"] == "active"
    assert on_live("enable", name, "--json").json()["error"]["code"] == "APP_ALREADY_ACTIVE"


def test_live_access_explain_names_the_grant(on_live, live):
    name = f"t{uuid.uuid4().hex[:12]}"
    assert on_live("apps", "create", name).code == 0
    none = on_live("access", "explain", name, "--json")
    assert none.code == 0, none.stdout
    assert (none.json()["allowed"], none.json()["reason"]) == (False, "no_grant")
    shared = on_live("share", name, "--org", "--json")
    grant = shared.json()["grants"][0]["id"]
    by_email = on_live("access", "explain", name, "dev@example.invalid", "--json")
    assert by_email.code == 0, by_email.stdout
    result = AccessResult.model_validate(by_email.json())
    assert (result.user_id, result.allowed, result.role) == (live.admin_id, True, "user")
    assert [g.grant_id for g in result.grants] == [grant]
    mine = on_live("apps", "--mine", "--json")
    assert name in [a["slug"] for a in mine.json()["apps"]]
