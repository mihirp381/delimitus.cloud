"""``deploy``, ``releases``, ``rollback`` and ``promote`` on a scripted API; the bundle upload."""

import hashlib
import json
from pathlib import Path

import httpx2
import pytest

from ssc_bundle.limits import BundleTooLargeError
from ssc_cli.commands import deploy as deploy_module
from ssc_cli.commands.deploy import WAKING
from ssc_cli.credentials import SERVICE
from ssc_cli.errors import ExitCode
from ssc_cli.session import Session
from ssc_cli.shapes import (
    DeployResult,
    ErrorResult,
    PromoteResult,
    ReleasesResult,
    RollbackResult,
)

API = "https://api.test"
APP_ID = "app_aaaaaaaaaaaaaaaaaaaa"
USR = "usr_aaaaaaaaaaaaaaaaaaaa"
PROD = "env_prodprodprodprodprod"
PREVIEW = "env_prevprevprevprevprev"
BUNDLE = "bdl_aaaaaaaaaaaaaaaaaaaa"
BUILD = "bld_aaaaaaaaaaaaaaaaaaaa"
REL = "rel_aaaaaaaaaaaaaaaaaaaa"
DEP = "dep_aaaaaaaaaaaaaaaaaaaa"
PREVIEW_URL = "https://demo--preview.qtbvkmrdhpsc.delimitusapps.com"
PROD_URL = "https://demo.qtbvkmrdhpsc.delimitusapps.com"
SIGNATURE = "sig-do-not-print-0123456789"
UPLOAD_URL = f"https://blobs.test/put/{BUNDLE}?sig={SIGNATURE}"
AWS_KEY = "AKIA" + "QWERTYUIOPASDFGH"
BUNDLES = f"/v1/apps/{APP_ID}/bundles"
BUILDS = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/builds"
PREVIEW_DEPLOYMENTS = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/deployments"
PROD_DEPLOYMENTS = f"/v1/apps/{APP_ID}/environments/{PROD}/deployments"
RELEASES = f"/v1/apps/{APP_ID}/releases"


def _no_sleep(_: float) -> None:
    return None


def _app(*, preview_live: str | None = None, prod_live: str | None = None) -> dict[str, object]:
    return {
        "id": APP_ID,
        "slug": "demo",
        "owner_user_id": USR,
        "status": "active",
        "created_at": "2026-09-29T00:00:00Z",
        "environments": [
            {
                "id": PREVIEW,
                "name": "preview",
                "config_version": 1,
                "grants_version": 1,
                "current_deployment_id": preview_live,
                "url": PREVIEW_URL,
            },
            {
                "id": PROD,
                "name": "prod",
                "config_version": 1,
                "grants_version": 1,
                "current_deployment_id": prod_live,
                "url": PROD_URL,
            },
        ],
    }


def _bundle(state: str, *, upload: bool) -> dict[str, object]:
    body: dict[str, object] = {
        "bundle_id": BUNDLE,
        "app_id": APP_ID,
        "digest": "sha256:" + "0" * 64,
        "size_bytes": 1,
        "state": state,
        "manifest_digest": None,
        "source_commit": None,
        "created_at": "2026-09-29T00:00:00Z",
        "upload": None,
    }
    if upload:
        body["upload"] = {
            "method": "PUT",
            "url": UPLOAD_URL,
            "headers": {"content-type": "application/gzip"},
            "expires_at": "2026-09-29T00:10:00Z",
        }
    return body


CHANGE = {
    "severity": "approval",
    "kind": "internet_host",
    "subject": "api.example.com",
    "consequence": "Calls to api.example.com need an admin's approval in prod.",
    "approver": "an org admin",
}


def _build(state: str, *, failure_code: str | None = None) -> httpx2.Response:
    done = state == "succeeded"
    return httpx2.Response(
        200,
        json={
            "build_id": BUILD,
            "app_id": APP_ID,
            "environment_id": PREVIEW,
            "bundle_id": BUNDLE,
            "state": state,
            "release_id": REL if done else None,
            "release_number": 7 if done else None,
            "failure_code": failure_code,
            "capability_diff": {"changes": [], "total": 0},
            "created_at": "2026-09-29T00:00:00Z",
            "started_at": None,
            "finished_at": None,
        },
    )


def _operation(
    state: str,
    *,
    failure_code: str | None = None,
    env: str = PREVIEW,
    rel: str = REL,
    notice: str | None = None,
):
    return httpx2.Response(
        200,
        json={
            "operation_id": DEP,
            "kind": "deploy",
            "state": state,
            "app_id": APP_ID,
            "environment_id": env,
            "release_id": rel,
            "started_at": "2026-09-29T00:00:00Z",
            "finished_at": None,
            "failure_code": failure_code,
            "notice": notice,
        },
    )


def _accepted(notice: str | None = None) -> httpx2.Response:
    return httpx2.Response(
        202,
        json={"operation_id": DEP, "state": "pending", "notice": notice},
        headers={"location": f"/v1/operations/{DEP}"},
    )


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "ssc.toml").write_text('schema = "ssc/v1"\n')
    (root / "main.py").write_text("print('hello')\n")
    return root


@pytest.fixture
def api(fake_api, isolated):
    """The app, a pending bundle to upload, a build that succeeds on the second poll, and a
    deployment that is healthy on the second poll."""
    isolated.set_password(SERVICE, API, "tok")
    summary = {k: _app()[k] for k in ("id", "slug", "owner_user_id", "status")}
    fake_api.add("GET", "/v1/apps", httpx2.Response(200, json={"apps": [summary]}))
    fake_api.add("GET", f"/v1/apps/{APP_ID}", httpx2.Response(200, json=_app()))
    fake_api.add("POST", BUNDLES, httpx2.Response(201, json=_bundle("pending", upload=True)))
    fake_api.add("PUT", f"/put/{BUNDLE}", httpx2.Response(201))
    fake_api.add(
        "POST",
        f"{BUNDLES}/{BUNDLE}/complete",
        httpx2.Response(200, json=_bundle("stored", upload=False)),
    )
    fake_api.add(
        "POST",
        BUILDS,
        httpx2.Response(
            202,
            json={
                "build_id": BUILD,
                "state": "queued",
                "capability_diff": {"changes": [CHANGE], "total": 1},
            },
            headers={"location": f"/v1/builds/{BUILD}"},
        ),
    )
    fake_api.add("GET", f"/v1/builds/{BUILD}", _build("running"), _build("succeeded"))
    fake_api.add("POST", PREVIEW_DEPLOYMENTS, _accepted())
    fake_api.add("GET", f"/v1/operations/{DEP}", _operation("running"), _operation("healthy"))
    return fake_api


def _calls(fake_api) -> list[tuple[str, str]]:
    return [(r.method, r.url.path) for r in fake_api.seen]


def _body(request: httpx2.Request) -> dict[str, object]:
    return json.loads(request.content)


def _error(r) -> dict[str, object]:
    return ErrorResult.model_validate(r.json()).error.model_dump()


# ── deploy ──────────────────────────────────────────────────────────────────


def test_deploy_uploads_builds_and_deploys_to_preview(cli, api, folder):
    r = cli("deploy", str(folder), "--app", "demo", "--wait", "--json", session=api.session())
    assert r.code == 0, (r.stdout, r.stderr)
    assert r.stderr == ""
    result = DeployResult.model_validate(r.json())
    assert (result.environment, result.environment_id, result.state) == (
        "preview",
        PREVIEW,
        "healthy",
    )
    assert (result.build_id, result.release_id, result.release_number) == (BUILD, REL, 7)
    assert (result.operation_id, result.url, result.uploaded) == (DEP, PREVIEW_URL, True)
    assert [c.subject for c in result.capability_changes] == ["api.example.com"]
    assert result.warnings == []
    assert _calls(api) == [
        ("GET", "/v1/apps"),
        ("GET", f"/v1/apps/{APP_ID}"),
        ("POST", BUNDLES),
        ("PUT", f"/put/{BUNDLE}"),
        ("POST", f"{BUNDLES}/{BUNDLE}/complete"),
        ("POST", BUILDS),
        ("GET", f"/v1/builds/{BUILD}"),
        ("GET", f"/v1/builds/{BUILD}"),
        ("POST", PREVIEW_DEPLOYMENTS),
        ("GET", f"/v1/operations/{DEP}"),
        ("GET", f"/v1/operations/{DEP}"),
    ]
    asked, put, _, build, deployment = (
        api.seen[2],
        api.seen[3],
        api.seen[4],
        api.seen[5],
        api.seen[8],
    )
    assert _body(asked)["digest"] == result.digest
    assert _body(asked)["size_bytes"] == len(put.content)
    assert "sha256:" + hashlib.sha256(put.content).hexdigest() == result.digest
    assert put.url == httpx2.URL(UPLOAD_URL)
    assert "authorization" not in put.headers
    assert put.headers["content-type"] == "application/gzip"
    assert _body(build) == {"bundle_id": BUNDLE}
    assert _body(deployment) == {"release_id": REL, "kind": "deploy"}
    posts = [q for q in api.seen if q.method == "POST"]
    assert all(q.headers.get("idempotency-key") for q in posts)
    assert len({q.headers["idempotency-key"] for q in posts}) == len(posts)


def test_deploy_never_touches_prod(cli, api, folder):
    r = cli("deploy", str(folder), "--app", "demo", "--wait", "--json", session=api.session())
    assert r.code == 0
    assert not any(PROD in q.url.path for q in api.seen)


def test_deploy_without_wait_stops_after_the_deployment_is_accepted(cli, api, folder):
    r = cli("deploy", str(folder), "--app", APP_ID, "--json", session=api.session())
    assert r.code == 0, r.stdout
    assert DeployResult.model_validate(r.json()).state == "pending"
    assert ("GET", f"/v1/operations/{DEP}") not in _calls(api)
    assert ("GET", "/v1/apps") not in _calls(api)


def test_bytes_the_api_already_has_are_not_uploaded(cli, api, folder):
    api.routes[("POST", BUNDLES)] = [httpx2.Response(200, json=_bundle("stored", upload=False))]
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert r.code == 0, r.stdout
    assert DeployResult.model_validate(r.json()).uploaded is False
    assert not any(m == "PUT" or p.endswith("/complete") for m, p in _calls(api))


def test_a_commit_is_sent_with_the_bundle(cli, api, folder):
    commit = "ab" * 20
    r = cli("deploy", str(folder), "--app", "demo", "--commit", commit, session=api.session())
    assert r.code == 0, r.stderr
    assert _body(api.seen[2])["source_commit"] == commit


def test_deploy_human_output(cli, api, folder):
    r = cli("deploy", str(folder), "--app", "demo", "--wait", session=api.session())
    assert r.code == 0
    assert r.stdout == f"R7 is live in preview of demo.\nPreview: {PREVIEW_URL}\n{WAKING}\n"
    assert "Packed 2 files" in r.stderr
    assert "Uploading." in r.stderr
    assert CHANGE["consequence"] in r.stderr
    assert SIGNATURE not in r.stdout + r.stderr
    pending = cli("deploy", str(folder), "--app", "demo", session=api.session())
    assert pending.stdout.splitlines()[0] == f"Deploying R7 to preview of demo ({DEP})."


def test_waking_up_is_explained_only_on_the_first_deploy(cli, api, folder):
    api.routes[("GET", f"/v1/apps/{APP_ID}")] = [
        httpx2.Response(200, json=_app(preview_live="dep_bbbbbbbbbbbbbbbbbbbb"))
    ]
    r = cli("deploy", str(folder), "--app", "demo", "--wait", session=api.session())
    assert r.code == 0, r.stderr
    assert '"waking up"' not in r.stdout + r.stderr
    assert '"waking up" page' in WAKING


def test_the_first_deploy_of_a_database_says_what_it_sets_off_once(cli, api, folder):
    notice = "Creating your company's database, about ten minutes, this happens once."
    (folder / "ssc.toml").write_text('schema = "ssc/v1"\n[state]\npostgres = true\n')
    api.routes[("POST", PREVIEW_DEPLOYMENTS)] = [_accepted(notice)]
    api.routes[("GET", f"/v1/operations/{DEP}")] = [
        _operation("running", notice=notice),
        _operation("healthy"),
    ]
    r = cli("deploy", str(folder), "--app", "demo", "--wait", session=api.session())
    assert r.code == 0, r.stderr
    assert r.stderr.count(notice) == 1
    pending = cli("deploy", str(folder), "--app", "demo", session=api.session())
    assert pending.stderr.count(notice) == 1
    quiet = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert DeployResult.model_validate(quiet.json()).notice == notice
    assert notice not in quiet.stderr


def test_a_streamlit_folder_still_deploys(cli, api, folder):
    (folder / "app.py").write_text("import streamlit as st\nst.title('hi')\n")
    (folder / "requirements.txt").write_text("streamlit\n")
    start = '[runtime]\nstart = "streamlit run app.py --server.port $PORT"\n'
    (folder / "ssc.toml").write_text('schema = "ssc/v1"\n' + start)
    r = cli("deploy", str(folder), "--app", "demo", "--wait", "--json", session=api.session())
    assert r.code == 0, r.stdout
    assert DeployResult.model_validate(r.json()).state == "healthy"


def test_a_deploy_waiting_for_the_company_database_says_so_once(cli, api, folder):
    notice = "Creating your company's database, about ten minutes, this happens once."
    api.routes[("GET", f"/v1/operations/{DEP}")] = [
        _operation("running", notice=notice),
        _operation("running", notice=notice),
        _operation("running"),
        _operation("healthy"),
    ]
    r = cli("deploy", str(folder), "--app", "demo", "--wait", session=api.session())
    assert r.code == 0, r.stderr
    assert r.stderr.count(notice) == 1
    quiet = cli("deploy", str(folder), "--app", "demo", "--wait", "--json", session=api.session())
    assert notice not in quiet.stdout + quiet.stderr


def test_a_warning_is_shown_and_does_not_block(cli, api, folder):
    (folder / "settings.py").write_text('DB = "postgresql://app:hunter2hunter2@localhost/app"\n')
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert r.code == 0, r.stdout
    warnings = DeployResult.model_validate(r.json()).warnings
    assert [(w.path, w.line) for w in warnings] == [("settings.py", 1)]
    assert "hunter2hunter2" not in r.stdout
    human = cli("deploy", str(folder), "--app", "demo", session=api.session())
    assert "Warning: " in human.stderr
    assert "settings.py:1" in human.stderr


def _secret(root: Path) -> None:
    (root / "config.py").write_text(f'KEY = "{AWS_KEY}"\n')


def _bad_manifest(root: Path) -> None:
    (root / "ssc.toml").write_text('schema = "ssc/v0"\n')


def _link(root: Path) -> None:
    (root / "elsewhere").symlink_to("/etc/hosts")


def _sqlite(root: Path) -> None:
    (root / "db.py").write_text("import sqlite3\ndb = sqlite3.connect('data.db')\n")


@pytest.mark.parametrize(
    ("spoil", "code"),
    [
        (_secret, "SECRET_IN_BUNDLE"),
        (_bad_manifest, "MANIFEST_INVALID"),
        (_link, "BUNDLE_MALFORMED"),
        (_sqlite, "STATE_SQLITE_EPHEMERAL"),
    ],
)
def test_local_refusals_exit_4_before_any_request(cli, api, folder, spoil, code):
    spoil(folder)
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert r.code == ExitCode.BLOCKED, r.stdout
    error = _error(r)
    assert (error["code"], error["status"]) == (code, None)
    assert api.seen == []
    assert AWS_KEY not in r.stdout
    human = cli("deploy", str(folder), "--app", "demo", session=api.session())
    assert human.code == ExitCode.BLOCKED
    assert f"Code: {code}" in human.stderr
    assert AWS_KEY not in human.stderr


def test_a_secret_is_named_by_file_and_line(cli, api, folder):
    _secret(folder)
    r = cli("deploy", str(folder), "--app", "demo", session=api.session())
    assert "config.py:1" in r.stderr
    assert "Fix: " in r.stderr


def test_too_large_exits_4(cli, api, folder, monkeypatch):
    def too_large(root: Path, dest: Path):
        raise BundleTooLargeError("bytes", "the bundle is 101 MiB; the cap is 100 MiB")

    monkeypatch.setattr(deploy_module, "prepare", too_large)
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert r.code == ExitCode.BLOCKED
    assert _error(r)["code"] == "BUNDLE_TOO_LARGE"
    assert api.seen == []


@pytest.mark.parametrize(
    "args",
    [
        ("deploy",),
        ("deploy", ".", "--app", "demo", "--commit", "abc123"),
        ("deploy", ".", "--app", "demo", "--commit", "AB" * 20),
        ("deploy", ".", "--app", "demo", "--env", "prod"),
        ("deploy", "/nonexistent-ssc-folder", "--app", "demo"),
        ("deploy", ".", "--app", "demo", "--timeout", "0"),
        ("deploy", "--app", "demo", "--build", "bld_x"),
        ("deploy", "--app", "demo", "--build", BUILD, "--commit", "ab" * 20),
        ("releases",),
        ("releases", "demo", "--limit", "101"),
        ("releases", "demo", "--before", "0"),
        ("rollback", "demo"),
        ("rollback", "demo", "latest"),
        ("rollback", "demo", "R0"),
        ("rollback", "demo", "rel"),
        ("rollback", "demo", "R1.5"),
    ],
)
def test_usage_errors_exit_2_before_any_request(cli, api, args):
    r = cli(*args, "--json", session=api.session())
    assert r.code == ExitCode.USAGE, (r.stdout, r.stderr)
    assert api.seen == []


def test_a_failed_build_names_its_code_and_build(cli, api, folder):
    api.routes[("GET", f"/v1/builds/{BUILD}")] = [
        _build("running"),
        _build("failed", failure_code="MANIFEST_INVALID"),
    ]
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert r.code == ExitCode.FAILED
    error = _error(r)
    assert (error["code"], error["status"], error["instance"]) == (
        "MANIFEST_INVALID",
        None,
        f"/v1/builds/{BUILD}",
    )
    assert ("POST", PREVIEW_DEPLOYMENTS) not in _calls(api)
    human = cli("deploy", str(folder), "--app", "demo", session=api.session())
    assert "Fix: Run `ssc doctor`" in human.stderr


def test_sqlite_on_disk_is_refused_before_upload_with_the_postgres_fix(cli, api, folder):
    _sqlite(folder)
    r = cli("deploy", str(folder), "--app", "demo", session=api.session())
    assert r.code == ExitCode.BLOCKED
    assert "a SQLite connect on a file at db.py" in r.stderr
    assert "[state]\npostgres = true" in r.stderr
    assert api.seen == []
    (folder / "tests").mkdir()
    (folder / "db.py").rename(folder / "tests" / "test_db.py")
    (folder / "mem.py").write_text("db = sqlite3.connect(':memory:')\n")
    assert cli("deploy", str(folder), "--app", "demo", session=api.session()).code == 0


def test_a_sqlite_build_failure_shows_the_postgres_fix(cli, api, folder):
    api.routes[("GET", f"/v1/builds/{BUILD}")] = [
        _build("failed", failure_code="STATE_SQLITE_EPHEMERAL")
    ]
    human = cli("deploy", str(folder), "--app", "demo", session=api.session())
    assert human.code == ExitCode.FAILED
    assert "postgres = true" in human.stderr


def test_a_build_failed_without_a_code(cli, api, folder):
    api.routes[("GET", f"/v1/builds/{BUILD}")] = [_build("failed")]
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert (r.code, _error(r)["code"]) == (ExitCode.FAILED, "BUILD_FAILED")


def test_a_succeeded_build_without_a_release_is_a_bad_response(cli, api, folder):
    broken = _build("succeeded")
    body = json.loads(broken.content)
    body["release_id"] = None
    api.routes[("GET", f"/v1/builds/{BUILD}")] = [httpx2.Response(200, json=body)]
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert (r.code, _error(r)["code"]) == (ExitCode.FAILED, "BAD_RESPONSE")


@pytest.mark.parametrize(
    ("state", "failure_code", "code", "fix"),
    [
        ("failed", "HEALTH_CHECK_FAILED", "HEALTH_CHECK_FAILED", "$PORT"),
        ("failed", "APPROVAL_REQUIRED", "APPROVAL_REQUIRED", "approval requests"),
        ("failed", "SNAPSHOT_UNCONFIRMED", "SNAPSHOT_UNCONFIRMED", "still serving"),
        ("failed", None, "DEPLOYMENT_FAILED", None),
        ("superseded", None, "DEPLOYMENT_SUPERSEDED", None),
    ],
)
def test_a_deployment_that_ends_badly(cli, api, folder, state, failure_code, code, fix):
    api.routes[("GET", f"/v1/operations/{DEP}")] = [_operation(state, failure_code=failure_code)]
    r = cli("deploy", str(folder), "--app", "demo", "--wait", "--json", session=api.session())
    assert r.code == ExitCode.FAILED
    error = _error(r)
    assert (error["code"], error["status"], error["instance"]) == (
        code,
        None,
        f"/v1/operations/{DEP}",
    )
    human = cli("deploy", str(folder), "--app", "demo", "--wait", session=api.session())
    assert (fix in human.stderr) if fix else ("Fix:" not in human.stderr)


def test_waiting_gives_up_after_the_timeout(cli, api, folder):
    api.routes[("GET", f"/v1/builds/{BUILD}")] = [_build("running")]
    slept: list[float] = []
    session = Session(
        api_override=API, transport=httpx2.MockTransport(api.handler), sleep=slept.append
    )
    r = cli("deploy", str(folder), "--app", "demo", "--timeout", "4", "--json", session=session)
    assert r.code == ExitCode.FAILED
    error = _error(r)
    assert (error["code"], error["instance"]) == ("WAIT_TIMED_OUT", f"/v1/builds/{BUILD}")
    assert f"`ssc deploy --app demo --build {BUILD}`" in str(error["detail"])
    assert "ssc status" not in str(error["detail"])
    assert slept == [2.0, 2.0]
    assert _calls(api).count(("GET", f"/v1/builds/{BUILD}")) == 3
    assert ("POST", PREVIEW_DEPLOYMENTS) not in _calls(api)


def test_one_timeout_covers_the_build_and_the_deployment(cli, api, folder):
    api.routes[("GET", f"/v1/builds/{BUILD}")] = [_build("running"), _build("succeeded")]
    api.routes[("GET", f"/v1/operations/{DEP}")] = [_operation("running")]
    slept: list[float] = []
    session = Session(
        api_override=API, transport=httpx2.MockTransport(api.handler), sleep=slept.append
    )
    r = cli(
        "deploy",
        str(folder),
        "--app",
        "demo",
        "--wait",
        "--timeout",
        "6",
        "--json",
        session=session,
    )
    assert r.code == ExitCode.FAILED
    error = _error(r)
    assert (error["code"], error["instance"]) == ("WAIT_TIMED_OUT", f"/v1/operations/{DEP}")
    assert "after 6 seconds" in str(error["detail"])
    assert "`ssc status demo`" in str(error["detail"])
    assert slept == [2.0, 2.0, 2.0]
    assert _calls(api).count(("GET", f"/v1/operations/{DEP}")) == 3


def _resumable(api) -> None:
    api.add(
        "GET",
        f"{RELEASES}/{REL}",
        httpx2.Response(200, json=_release(7, PREVIEW) | {"release_id": REL}),
    )


def test_the_resume_command_waits_for_the_build_and_deploys_its_release(cli, api, tmp_path):
    _resumable(api)
    r = cli(
        "deploy",
        str(tmp_path),
        "--app",
        "demo",
        "--build",
        BUILD,
        "--wait",
        "--json",
        session=api.session(),
    )
    assert r.code == 0, (r.stdout, r.stderr)
    result = DeployResult.model_validate(r.json())
    assert (result.build_id, result.bundle_id, result.release_id) == (BUILD, BUNDLE, REL)
    assert (result.digest, result.uploaded, result.state) == (
        "sha256:" + "3" * 64,
        False,
        "healthy",
    )
    assert (result.warnings, result.capability_changes) == ([], [])
    assert _calls(api) == [
        ("GET", "/v1/apps"),
        ("GET", f"/v1/apps/{APP_ID}"),
        ("GET", f"/v1/builds/{BUILD}"),
        ("GET", f"/v1/builds/{BUILD}"),
        ("GET", f"{RELEASES}/{REL}"),
        ("POST", PREVIEW_DEPLOYMENTS),
        ("GET", f"/v1/operations/{DEP}"),
        ("GET", f"/v1/operations/{DEP}"),
    ]
    assert _body(api.seen[5]) == {"release_id": REL, "kind": "deploy"}


def test_resuming_a_build_of_another_environment_is_refused(cli, api):
    body = json.loads(_build("succeeded").content)
    body["environment_id"] = PROD
    api.routes[("GET", f"/v1/builds/{BUILD}")] = [httpx2.Response(200, json=body)]
    r = cli("deploy", "--app", "demo", "--build", BUILD, "--json", session=api.session())
    assert (r.code, _error(r)["code"]) == (ExitCode.FAILED, "BUILD_NOT_FOUND")
    assert ("POST", PREVIEW_DEPLOYMENTS) not in _calls(api)


def test_a_superseded_deployment_says_why(cli, api, folder):
    api.routes[("GET", f"/v1/operations/{DEP}")] = [_operation("superseded")]
    r = cli("deploy", str(folder), "--app", "demo", "--wait", "--json", session=api.session())
    assert str(_error(r)["detail"]).endswith("what the environment runs or will run.")


def test_api_refusals_pass_through(cli, api, folder, fake_problem):
    api.routes[("POST", BUILDS)] = [fake_problem(409, "BUILD_IN_FLIGHT")]
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert r.code == ExitCode.FAILED
    assert (_error(r)["code"], _error(r)["status"]) == ("BUILD_IN_FLIGHT", 409)


def test_an_unknown_app_uploads_nothing(cli, api, folder):
    r = cli("deploy", str(folder), "--app", "nope", "--json", session=api.session())
    assert (r.code, _error(r)["code"]) == (ExitCode.FAILED, "APP_NOT_FOUND")
    assert ("POST", BUNDLES) not in _calls(api)


def test_a_pending_bundle_without_an_upload_address_is_a_bad_response(cli, api, folder):
    api.routes[("POST", BUNDLES)] = [httpx2.Response(201, json=_bundle("pending", upload=False))]
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert (r.code, _error(r)["code"]) == (ExitCode.FAILED, "BAD_RESPONSE")


# ── the upload ──────────────────────────────────────────────────────────────


def test_a_refused_upload_never_shows_the_address(cli, api, folder):
    api.routes[("PUT", f"/put/{BUNDLE}")] = [httpx2.Response(403, text=UPLOAD_URL)]
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert r.code == ExitCode.FAILED
    assert _error(r)["code"] == "UPLOAD_FAILED"
    assert SIGNATURE not in r.stdout
    assert ("POST", f"{BUNDLES}/{BUNDLE}/complete") not in _calls(api)


def test_an_upload_is_retried_after_a_server_error(cli, api, folder):
    api.routes[("PUT", f"/put/{BUNDLE}")] = [httpx2.Response(503), httpx2.Response(201)]
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert r.code == 0, r.stdout
    puts = [q for q in api.seen if q.method == "PUT"]
    assert len(puts) == 2
    assert puts[0].content == puts[1].content


def test_an_upload_that_cannot_connect_exits_5(cli, api, folder):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "blobs.test":
            raise httpx2.ConnectError(f"cannot reach {request.url}", request=request)
        return api.handler(request)

    session = Session(api_override=API, transport=httpx2.MockTransport(handler), sleep=_no_sleep)
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=session)
    assert r.code == ExitCode.NETWORK
    assert _error(r)["code"] == "NETWORK_ERROR"
    assert SIGNATURE not in r.stdout
    human = cli("deploy", str(folder), "--app", "demo", session=session)
    assert SIGNATURE not in human.stderr


# ── releases ────────────────────────────────────────────────────────────────


def _release(number: int, built_for: str | None, *, commit: str | None = None) -> dict[str, object]:
    return {
        "release_id": f"rel_{number:020d}",
        "number": number,
        "label": f"R{number}",
        "image_digest": "sha256:" + "1" * 64,
        "manifest_digest": "sha256:" + "2" * 64,
        "source_digest": "sha256:" + "3" * 64,
        "source_commit": commit,
        "built_for_environment_id": built_for,
        "created_at": "2026-09-29T00:00:00Z",
        "actor": {"kind": "user", "id": USR, "via_agent": number == 3},
    }


@pytest.fixture
def history(fake_api, isolated):
    isolated.set_password(SERVICE, API, "tok")
    live = _app(preview_live="dep_preview0000000000000", prod_live="dep_prod000000000000000")
    fake_api.add("GET", f"/v1/apps/{APP_ID}", httpx2.Response(200, json=live))
    fake_api.add(
        "GET",
        "/v1/operations/dep_preview0000000000000",
        _operation("healthy", rel="rel_00000000000000000003"),
    )
    fake_api.add(
        "GET",
        "/v1/operations/dep_prod000000000000000",
        _operation("healthy", env=PROD, rel="rel_00000000000000000002"),
    )
    page = {
        "items": [
            _release(3, PREVIEW, commit="c" * 40),
            _release(2, PROD),
            _release(1, None),
        ],
        "next_before": 1,
    }
    fake_api.add("GET", RELEASES, httpx2.Response(200, json=page))
    return fake_api


def test_releases_says_where_each_was_built_for_and_runs(cli, history):
    r = cli("releases", APP_ID, "--json", session=history.session())
    assert r.code == 0, r.stdout
    result = ReleasesResult.model_validate(r.json())
    assert [(x.label, x.built_for, x.live_in, x.via_agent) for x in result.releases] == [
        ("R3", "preview", ["preview"], True),
        ("R2", "prod", ["prod"], False),
        ("R1", None, [], False),
    ]
    assert result.next_before == 1
    listed = [q for q in history.seen if q.url.path == RELEASES]
    assert dict(listed[0].url.params) == {"limit": "20"}


def test_releases_pages_with_before(cli, history):
    r = cli("releases", APP_ID, "--limit", "3", "--before", "4", session=history.session())
    assert r.code == 0
    listed = [q for q in history.seen if q.url.path == RELEASES]
    assert dict(listed[0].url.params) == {"limit": "3", "before": "4"}
    lines = r.stdout.splitlines()
    assert lines[0].split()[:4] == ["RELEASE", "ID", "BUILT", "FOR"]
    assert lines[1].split()[:4] == ["R3", "rel_00000000000000000003", "preview", "preview"]
    assert "cccccccccccc " in lines[1]
    assert "(agent)" in lines[1]
    assert lines[-1] == "More: ssc releases demo --before 1"


def test_no_releases_yet(cli, history):
    history.routes[("GET", RELEASES)] = [
        httpx2.Response(200, json={"items": [], "next_before": None})
    ]
    r = cli("releases", APP_ID, session=history.session())
    assert r.stdout == "demo has no releases yet. Run `ssc deploy --app demo`.\n"


# ── rollback ────────────────────────────────────────────────────────────────


def _page(*releases: dict[str, object]) -> httpx2.Response:
    return httpx2.Response(200, json={"items": list(releases), "next_before": None})


@pytest.mark.parametrize("ref", ["R2", "r2", "2"])
def test_rollback_by_number_goes_to_the_environment_it_was_built_for(cli, history, ref):
    history.routes[("GET", RELEASES)] = [_page(_release(2, PROD))]
    history.add("POST", PROD_DEPLOYMENTS, _accepted())
    r = cli("rollback", APP_ID, ref, "--json", session=history.session())
    assert r.code == 0, r.stdout
    result = RollbackResult.model_validate(r.json())
    assert (result.environment, result.release_number, result.state) == ("prod", 2, "pending")
    assert result.url == PROD_URL
    listed = [q for q in history.seen if q.url.path == RELEASES]
    assert dict(listed[0].url.params) == {"limit": "1", "before": "3"}
    posted = [q for q in history.seen if q.method == "POST"]
    assert [q.url.path for q in posted] == [PROD_DEPLOYMENTS]
    assert _body(posted[0]) == {"release_id": "rel_00000000000000000002", "kind": "rollback"}
    assert posted[0].headers.get("idempotency-key")


def test_rollback_by_id_and_wait(cli, history):
    history.add(
        "GET",
        f"{RELEASES}/rel_00000000000000000003",
        httpx2.Response(200, json=_release(3, PREVIEW)),
    )
    history.add("POST", PREVIEW_DEPLOYMENTS, _accepted())
    history.add("GET", f"/v1/operations/{DEP}", _operation("running"), _operation("healthy"))
    r = cli("rollback", APP_ID, "rel_00000000000000000003", "--wait", session=history.session())
    assert r.code == 0, r.stderr
    assert r.stdout == f"preview of demo runs R3 again.\nURL: {PREVIEW_URL}\n"


def test_rollback_to_a_number_that_does_not_exist(cli, history):
    history.routes[("GET", RELEASES)] = [_page(_release(2, PROD))]
    r = cli("rollback", APP_ID, "R5", "--json", session=history.session())
    assert (r.code, _error(r)["code"]) == (ExitCode.FAILED, "RELEASE_NOT_FOUND")
    assert not [q for q in history.seen if q.method == "POST"]
    empty = [_page()]
    history.routes[("GET", RELEASES)] = empty
    r = cli("rollback", APP_ID, "R1", "--json", session=history.session())
    assert _error(r)["code"] == "RELEASE_NOT_FOUND"


def test_rollback_to_an_unbuilt_release_needs_an_environment(cli, history):
    history.routes[("GET", RELEASES)] = [_page(_release(1, None))]
    r = cli("rollback", APP_ID, "R1", "--json", session=history.session())
    assert (r.code, _error(r)["code"]) == (ExitCode.USAGE, "ENVIRONMENT_REQUIRED")
    history.add("POST", PREVIEW_DEPLOYMENTS, _accepted())
    chosen = cli("rollback", APP_ID, "R1", "--env", "preview", "--json", session=history.session())
    assert chosen.code == 0, chosen.stdout
    assert RollbackResult.model_validate(chosen.json()).environment_id == PREVIEW


def test_rollback_to_an_unknown_environment(cli, history):
    history.routes[("GET", RELEASES)] = [_page(_release(2, "env_otherotherotherother"))]
    r = cli("rollback", APP_ID, "R2", "--json", session=history.session())
    assert _error(r)["code"] == "ENVIRONMENT_NOT_FOUND"
    r = cli("rollback", APP_ID, "R2", "--env", "staging", "--json", session=history.session())
    assert _error(r)["code"] == "ENVIRONMENT_NOT_FOUND"


def test_rollback_across_environments_is_the_apis_refusal(cli, history, fake_problem):
    history.routes[("GET", RELEASES)] = [_page(_release(2, PROD))]
    history.add("POST", PREVIEW_DEPLOYMENTS, fake_problem(409, "RELEASE_ENVIRONMENT_MISMATCH"))
    r = cli("rollback", APP_ID, "R2", "--env", "preview", "--json", session=history.session())
    assert (r.code, _error(r)["code"]) == (ExitCode.FAILED, "RELEASE_ENVIRONMENT_MISMATCH")


# ── promote ─────────────────────────────────────────────────────────────────

LIVE = "dep_livelivelivelivelive"
SOURCE = "rel_sourcesourcesources"
PROMOTE = f"/v1/apps/{APP_ID}/promote"


def _prod_build(state: str) -> httpx2.Response:
    body = json.loads(_build(state).content)
    body["environment_id"] = PROD
    return httpx2.Response(200, json=body)


@pytest.fixture
def promoting(fake_api, isolated):
    """Preview runs SOURCE healthy; promote queues a prod build that succeeds on the second poll,
    and the prod deployment is healthy on the second poll."""
    isolated.set_password(SERVICE, API, "tok")
    app = _app(preview_live=LIVE)
    summary = {k: app[k] for k in ("id", "slug", "owner_user_id", "status")}
    fake_api.add("GET", "/v1/apps", httpx2.Response(200, json={"apps": [summary]}))
    fake_api.add("GET", f"/v1/apps/{APP_ID}", httpx2.Response(200, json=app))
    fake_api.add("GET", f"/v1/operations/{LIVE}", _operation("healthy", rel=SOURCE))
    fake_api.add(
        "POST",
        PROMOTE,
        httpx2.Response(
            202,
            json={
                "build_id": BUILD,
                "state": "queued",
                "capability_diff": {"changes": [], "total": 0},
            },
            headers={"location": f"/v1/builds/{BUILD}"},
        ),
    )
    fake_api.add("GET", f"/v1/builds/{BUILD}", _prod_build("running"), _prod_build("succeeded"))
    fake_api.add("POST", PROD_DEPLOYMENTS, _accepted())
    fake_api.add(
        "GET",
        f"/v1/operations/{DEP}",
        _operation("running", env=PROD),
        _operation("healthy", env=PROD),
    )
    return fake_api


def test_promote_builds_for_prod_then_deploys_and_waits(cli, promoting):
    r = cli("promote", "demo", "--wait", "--json", session=promoting.session())
    assert r.code == 0, (r.stdout, r.stderr)
    result = PromoteResult.model_validate(r.json())
    assert (result.environment, result.environment_id, result.source_release_id) == (
        "prod",
        PROD,
        SOURCE,
    )
    assert (result.build_id, result.release_id, result.release_number) == (BUILD, REL, 7)
    assert (result.operation_id, result.state, result.url) == (DEP, "healthy", PROD_URL)
    assert result.next_command is None
    assert _calls(promoting) == [
        ("GET", "/v1/apps"),
        ("GET", f"/v1/apps/{APP_ID}"),
        ("GET", f"/v1/operations/{LIVE}"),
        ("POST", PROMOTE),
        ("GET", f"/v1/builds/{BUILD}"),
        ("GET", f"/v1/builds/{BUILD}"),
        ("POST", PROD_DEPLOYMENTS),
        ("GET", f"/v1/operations/{DEP}"),
        ("GET", f"/v1/operations/{DEP}"),
    ]
    posts = [q for q in promoting.seen if q.method == "POST"]
    assert [_body(q) for q in posts] == [
        {"preview_release_id": SOURCE},
        {"release_id": REL, "kind": "deploy"},
    ]
    keys = {q.headers["Idempotency-Key"] for q in posts}
    assert len(keys) == 2


def test_promote_prints_the_prod_url(cli, promoting):
    r = cli("promote", "demo", "--wait", session=promoting.session())
    assert r.code == 0, (r.stdout, r.stderr)
    out = r.stdout + r.stderr
    assert "prod of demo runs R7." in out
    assert f"Prod: {PROD_URL}" in out


def test_promote_without_wait_stops_after_the_build(cli, promoting):
    r = cli("promote", "demo", "--json", session=promoting.session())
    assert r.code == 0, (r.stdout, r.stderr)
    result = PromoteResult.model_validate(r.json())
    resume = f"ssc promote demo --build {BUILD} --wait"
    assert (result.build_id, result.release_id, result.operation_id) == (BUILD, REL, None)
    assert result.next_command == resume
    assert ("POST", PROD_DEPLOYMENTS) not in _calls(promoting)
    human = cli("promote", "demo", session=promoting.session())
    assert f"Built R7 for prod. Put it live with `{resume}`." in human.stdout + human.stderr


def test_promote_resumes_a_prod_build_and_deploys_it(cli, promoting):
    r = cli("promote", "demo", "--build", BUILD, "--wait", "--json", session=promoting.session())
    assert r.code == 0, (r.stdout, r.stderr)
    result = PromoteResult.model_validate(r.json())
    assert (result.source_release_id, result.state) == (None, "healthy")
    assert ("POST", PROMOTE) not in _calls(promoting)
    assert ("POST", PROD_DEPLOYMENTS) in _calls(promoting)


def test_promote_refuses_to_resume_a_preview_build(cli, promoting):
    promoting.routes[("GET", f"/v1/builds/{BUILD}")] = [_build("succeeded")]
    r = cli("promote", "demo", "--build", BUILD, "--json", session=promoting.session())
    assert (r.code, _error(r)["code"]) == (ExitCode.FAILED, "BUILD_NOT_FOUND")
    assert not [q for q in promoting.seen if q.method == "POST"]


def test_promote_timing_out_on_the_build_names_the_resume_command(cli, promoting):
    promoting.routes[("GET", f"/v1/builds/{BUILD}")] = [_prod_build("running")]
    r = cli("promote", "demo", "--wait", "--timeout", "2", "--json", session=promoting.session())
    assert r.code == ExitCode.FAILED
    error = _error(r)
    assert (error["code"], error["instance"]) == ("WAIT_TIMED_OUT", f"/v1/builds/{BUILD}")
    assert f"`ssc promote demo --build {BUILD} --wait`" in str(error["detail"])
    assert ("POST", PROD_DEPLOYMENTS) not in _calls(promoting)


def test_promote_held_for_approval_names_the_resume_command(cli, promoting):
    promoting.routes[("GET", f"/v1/operations/{DEP}")] = [
        _operation("failed", env=PROD, failure_code="APPROVAL_REQUIRED")
    ]
    r = cli("promote", "demo", "--wait", session=promoting.session())
    assert r.code == ExitCode.FAILED
    fix = next(line for line in r.stderr.splitlines() if line.startswith("Fix: "))
    assert f"then run `ssc promote demo --build {BUILD} --wait`" in fix


def _secrets(env: str, *names: str) -> httpx2.Response:
    items = [{"name": n, "version": "1", "updated_at": "2026-09-29T00:00:00Z"} for n in names]
    return httpx2.Response(200, json={"environment_id": env, "items": items})


def test_promote_refused_for_a_missing_prod_secret_names_it(cli, promoting, fake_problem):
    promoting.routes[("POST", PROMOTE)] = [fake_problem(409, "PROD_SECRET_MISSING")]
    preview = _secrets(PREVIEW, "API_TOKEN", "DATABASE_URL", "SMTP_PASSWORD", "STRIPE_KEY")
    promoting.add("GET", f"/v1/apps/{APP_ID}/environments/{PREVIEW}/secrets", preview)
    prod = _secrets(PROD, "DATABASE_URL", "STRIPE_KEY")
    promoting.add("GET", f"/v1/apps/{APP_ID}/environments/{PROD}/secrets", prod)
    r = cli("promote", "demo", session=promoting.session())
    assert r.code == ExitCode.FAILED
    fix = next(line for line in r.stderr.splitlines() if line.startswith("Fix: "))
    assert "Set API_TOKEN, SMTP_PASSWORD on prod" in fix
    assert "`ssc secret set demo API_TOKEN --env prod`" in fix
    assert ("GET", f"/v1/builds/{BUILD}") not in _calls(promoting)


def test_promote_with_nothing_live_is_the_apis_refusal(cli, promoting, fake_problem):
    promoting.routes[("GET", f"/v1/operations/{LIVE}")] = [_operation("running", rel=SOURCE)]
    promoting.routes[("POST", PROMOTE)] = [fake_problem(409, "NOTHING_TO_PROMOTE")]
    r = cli("promote", "demo", "--json", session=promoting.session())
    assert (r.code, _error(r)["code"]) == (ExitCode.FAILED, "NOTHING_TO_PROMOTE")
    (sent,) = [q for q in promoting.seen if q.method == "POST"]
    assert _body(sent) == {"preview_release_id": None}


def test_an_upload_sends_its_exact_length_once_and_the_asked_headers(cli, api, folder):
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert r.code == 0, (r.stdout, r.stderr)
    (put,) = [q for q in api.seen if q.method == "PUT"]
    assert put.headers.get_list("content-length") == [str(len(put.content))]
    assert "transfer-encoding" not in put.headers
    assert put.headers["content-type"] == "application/gzip"
    assert "authorization" not in put.headers


def test_an_object_already_in_the_bucket_goes_on_to_complete(cli, api, folder):
    api.routes[("PUT", f"/put/{BUNDLE}")] = [httpx2.Response(412)]
    r = cli("deploy", str(folder), "--app", "demo", "--json", session=api.session())
    assert r.code == 0, (r.stdout, r.stderr)
    assert ("POST", f"{BUNDLES}/{BUNDLE}/complete") in _calls(api)
