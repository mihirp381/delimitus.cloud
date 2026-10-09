"""``ssc repo`` (GA-7.3): connect, show and disconnect an app's GitHub repository against a fake
API. Values the API would refuse are refused here before any request."""

import json

import httpx2
import pytest

from ssc_cli.credentials import SERVICE
from ssc_cli.errors import ExitCode
from ssc_cli.shapes import ErrorResult, RepoDisconnected, RepoResult
from ssc_contracts.errors import CATALOGUE, ErrorCode, problem_type

USR = "usr_aaaaaaaaaaaaaaaaaaaa"
APP_ID = "app_aaaaaaaaaaaaaaaaaaaa"
GITHUB = f"/v1/apps/{APP_ID}/github"
CI = {"name": "CI / test", "workflow": ".github/workflows/ci.yml"}


def _app() -> dict[str, object]:
    envs = [
        {"id": "env_prevprevprevprevprev", "name": "preview", "config_version": 1},
        {"id": "env_prodprodprodprodprod", "name": "prod", "config_version": 1},
    ]
    return {
        "id": APP_ID,
        "slug": "demo",
        "owner_user_id": USR,
        "status": "active",
        "created_at": "2026-10-09T00:00:00Z",
        "environments": [e | {"grants_version": 1} for e in envs],
    }


def _link(*checks: dict[str, str], branch: str = "main") -> httpx2.Response:
    body = {
        "app_id": APP_ID,
        "repository": "acme/payroll",
        "repository_id": 4242,
        "branch": branch,
        "required_checks": list(checks),
        "check_name": "SSC / preview",
        "updated_at": "2026-10-09T00:00:00Z",
    }
    return httpx2.Response(200, json=body)


def _catalogue_problem(code: ErrorCode) -> httpx2.Response:
    """The problem body the API sends, with the catalogue's own title and detail."""
    entry = CATALOGUE[code]
    body = {
        "type": problem_type(code),
        "title": entry.title,
        "status": entry.status,
        "detail": entry.detail,
        "instance": GITHUB,
        "code": code.value,
        "request_id": "req_test",
    }
    return httpx2.Response(entry.status, json=body)


@pytest.fixture
def api(fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    fake_api.add("GET", f"/v1/apps/{APP_ID}", httpx2.Response(200, json=_app()))
    fake_api.add("GET", "/v1/apps", httpx2.Response(200, json={"apps": [_app()]}))
    return fake_api


def _to_github(api) -> list[httpx2.Request]:
    return [q for q in api.seen if q.url.path == GITHUB]


def _connect(cli, api, *extra: str):
    return cli("repo", "connect", "demo", "acme/payroll", *extra, session=api.session())


# ── connect ──────────────────────────────────────────────────────────────────


def test_connect_sends_only_the_repository_by_default(cli, api):
    api.add("PUT", GITHUB, _link())
    r = _connect(cli, api)
    assert r.code == 0, r.stderr
    (put,) = _to_github(api)
    assert put.method == "PUT"
    assert json.loads(put.content) == {
        "repository": "acme/payroll",
        "branch": None,
        "required_checks": [],
    }
    assert r.stdout.splitlines() == [
        "Connected demo to acme/payroll, branch main.",
        'Every push to main deploys preview and reports on the commit as "SSC / preview".',
        "No required checks.",
    ]


def test_connect_sends_the_branch_and_each_check(cli, api):
    lint = {"name": "lint", "workflow": ".github/workflows/lint.yaml"}
    api.add("PUT", GITHUB, _link(CI, lint, branch="release/1"))
    r = _connect(
        cli,
        api,
        "--branch",
        "release/1",
        "--check",
        ".github/workflows/ci.yml CI / test",
        "--check",
        "  .github/workflows/lint.yaml \t lint ",
    )
    assert r.code == 0, r.stderr
    (put,) = _to_github(api)
    assert json.loads(put.content) == {
        "repository": "acme/payroll",
        "branch": "release/1",
        "required_checks": [CI, lint],
    }
    assert r.stdout.splitlines()[2:] == [
        "Required before promote:",
        "  .github/workflows/ci.yml CI / test",
        "  .github/workflows/lint.yaml lint",
    ]


def test_connect_json_is_the_repo_shape(cli, api):
    api.add("PUT", GITHUB, _link(CI))
    r = _connect(cli, api, "--check", ".github/workflows/ci.yml CI / test", "--json")
    assert r.code == 0, r.stderr
    out = RepoResult.model_validate(r.json())
    assert (out.app_id, out.slug, out.repository, out.repository_id, out.branch) == (
        APP_ID,
        "demo",
        "acme/payroll",
        4242,
        "main",
    )
    assert [c.model_dump() for c in out.required_checks] == [
        {"workflow": ".github/workflows/ci.yml", "name": "CI / test"}
    ]
    assert out.check_name == "SSC / preview"


def test_connect_help_says_it_replaces_the_whole_link(cli):
    r = cli("repo", "connect", "--help")
    assert r.code == 0
    text = " ".join(r.stdout.split())
    assert "replaces the whole link" in text
    assert "leaving --check out clears them" in text


@pytest.mark.parametrize(
    "args",
    [
        ("acme",),
        ("acme/pay roll",),
        ("acme/payroll/extra",),
        ("acme_x/payroll",),
        ("acme/payroll", "--branch", "has space"),
        ("acme/payroll", "--check", "ci.yml test"),
        ("acme/payroll", "--check", ".github/ci.yml test"),
        ("acme/payroll", "--check", ".github/workflows/ci.json test"),
        ("acme/payroll", "--check", ".github/workflows/ci.yml"),
        ("acme/payroll", "--check", ".github/workflows/ci.yml   "),
        ("acme/payroll", "--check", f".github/workflows/ci.yml {'x' * 201}"),
        ("acme/payroll", "--check", ".github/workflows/ci.yml bad\x07name"),
    ],
)
def test_a_bad_value_is_refused_before_any_request(cli, api, args):
    r = cli("repo", "connect", "demo", *args, session=api.session())
    assert r.code == ExitCode.USAGE, r.stdout
    assert api.seen == []


def test_an_eleventh_check_is_refused_before_any_request(cli, api):
    checks = [a for i in range(11) for a in ("--check", f".github/workflows/ci.yml job {i}")]
    r = _connect(cli, api, *checks)
    assert r.code == ExitCode.USAGE
    assert "at most 10" in r.stderr
    assert api.seen == []
    api.add("PUT", GITHUB, _link())
    assert _connect(cli, api, *checks[:20]).code == 0
    (put,) = _to_github(api)
    assert len(json.loads(put.content)["required_checks"]) == 10


def test_a_repository_the_app_cannot_reach_says_how_to_install_it(cli, api):
    api.add("PUT", GITHUB, _catalogue_problem(ErrorCode.REPOSITORY_NOT_INSTALLED))
    r = _connect(cli, api)
    assert r.code == ExitCode.FAILED
    entry = CATALOGUE[ErrorCode.REPOSITORY_NOT_INSTALLED]
    assert r.stderr.splitlines()[:3] == [
        f"Error: {entry.title}",
        entry.detail,
        "Code: REPOSITORY_NOT_INSTALLED",
    ]
    machine = _connect(cli, api, "--json")
    assert machine.code == ExitCode.FAILED
    error = ErrorResult.model_validate(machine.json()).error
    assert (error.code, error.status) == ("REPOSITORY_NOT_INSTALLED", 409)


def test_github_unavailable_is_reported_and_the_put_is_not_retried(cli, api):
    api.add("PUT", GITHUB, _catalogue_problem(ErrorCode.GITHUB_UNAVAILABLE))
    r = _connect(cli, api, "--json")
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "GITHUB_UNAVAILABLE"
    assert len(_to_github(api)) == 1
    human = _connect(cli, api)
    assert CATALOGUE[ErrorCode.GITHUB_UNAVAILABLE].detail in human.stderr


def test_an_agent_session_is_told_a_person_connects(cli, api, fake_problem):
    api.add("PUT", GITHUB, fake_problem(403, "AGENT_SESSION_REFUSED"))
    r = _connect(cli, api)
    assert r.code == ExitCode.FAILED
    assert "Fix: a person connects repositories" in r.stderr
    machine = _connect(cli, api, "--json")
    assert "Fix" not in machine.stdout
    assert machine.json()["error"]["code"] == "AGENT_SESSION_REFUSED"


def test_a_non_builder_is_told_who_connects(cli, api, fake_problem):
    api.add("PUT", GITHUB, fake_problem(403, "FORBIDDEN"))
    r = _connect(cli, api)
    assert r.code == ExitCode.FAILED
    assert "Fix: only an org admin, the app's owner or a builder on prod" in r.stderr


# ── show ─────────────────────────────────────────────────────────────────────


def test_show_renders_the_link(cli, api):
    api.add("GET", GITHUB, _link(CI))
    r = cli("repo", "show", APP_ID, session=api.session())
    assert r.code == 0, r.stderr
    assert r.stdout.splitlines() == [
        "demo is connected to acme/payroll, branch main.",
        'Every push to main deploys preview and reports on the commit as "SSC / preview".',
        "Required before promote:",
        "  .github/workflows/ci.yml CI / test",
    ]
    machine = cli("repo", "show", APP_ID, "--json", session=api.session())
    assert machine.code == 0
    out = RepoResult.model_validate(machine.json())
    assert out.required_checks[0].name == "CI / test"
    assert all(q.method == "GET" for q in api.seen)


def test_show_of_an_unconnected_app_is_not_found(cli, api, fake_problem):
    api.add("GET", GITHUB, fake_problem(404, "NOT_FOUND"))
    r = cli("repo", "show", "demo", session=api.session())
    assert r.code == ExitCode.FAILED
    assert "Code: NOT_FOUND" in r.stderr
    assert "Fix: connect one with `ssc repo connect demo OWNER/NAME`." in r.stderr
    machine = cli("repo", "show", "demo", "--json", session=api.session())
    assert machine.code == ExitCode.FAILED
    error = ErrorResult.model_validate(machine.json()).error
    assert (error.code, error.status) == ("NOT_FOUND", 404)


# ── disconnect ───────────────────────────────────────────────────────────────


def test_disconnect_sends_delete(cli, api):
    api.add("DELETE", GITHUB, httpx2.Response(204))
    r = cli("repo", "disconnect", "demo", session=api.session())
    assert r.code == 0, r.stderr
    assert r.stdout == (
        "Disconnected demo from its repository. "
        "Pushes no longer deploy preview; what is deployed stays.\n"
    )
    machine = cli("repo", "disconnect", "demo", "--json", session=api.session())
    assert machine.code == 0
    out = RepoDisconnected.model_validate(machine.json())
    assert (out.app_id, out.slug, out.disconnected) == (APP_ID, "demo", True)
    assert [q.method for q in _to_github(api)] == ["DELETE", "DELETE"]


def test_disconnect_of_an_unconnected_app_is_not_found(cli, api, fake_problem):
    api.add("DELETE", GITHUB, fake_problem(404, "NOT_FOUND"))
    r = cli("repo", "disconnect", "demo", "--json", session=api.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "NOT_FOUND"
