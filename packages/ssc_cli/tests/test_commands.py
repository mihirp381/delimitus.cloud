"""Every command: the registered set, ``--json``, refusals, exit codes and ``share``'s retries."""

import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import httpx2
import pytest
from pydantic import BaseModel
from typer.core import TyperGroup
from typer.main import get_command

from ssc_cli import __version__
from ssc_cli.credentials import SERVICE
from ssc_cli.errors import ExitCode
from ssc_cli.main import app
from ssc_cli.session import Session
from ssc_cli.shapes import (
    AppResult,
    AppsResult,
    DoctorResult,
    ErrorResult,
    InitResult,
    ShareResult,
    TokenClearResult,
    TokenSetResult,
    WhoamiResult,
)

CLEAN = Path(__file__).resolve().parent / "fixtures" / "doctor" / "clean"
ORG = "org_aaaaaaaaaaaaaaaaaaaa"
USR = "usr_aaaaaaaaaaaaaaaaaaaa"
APP_ID = "app_aaaaaaaaaaaaaaaaaaaa"
PROD = "env_prodprodprodprodprod"
PREVIEW = "env_prevprevprevprevprev"
ALLOWED = {"whoami", "token", "apps", "status", "share", "unshare", "doctor", "init"}


def slug() -> str:
    return f"t{uuid.uuid4().hex[:12]}"


def _no_sleep(_: float) -> None:
    return None


# ── the command set ─────────────────────────────────────────────────────────


def _paths() -> set[tuple[str, ...]]:
    root = get_command(app)
    assert isinstance(root, TyperGroup)
    out: set[tuple[str, ...]] = set()
    for name, cmd in root.commands.items():
        if isinstance(cmd, TyperGroup):
            if cmd.invoke_without_command:
                out.add((name,))
            out |= {(name, sub) for sub in cmd.commands}
        else:
            out.add((name,))
    return out


def test_help_lists_exact_set(cli):
    r = cli("--help")
    assert r.code == 0
    listed = r.stdout.split("Commands:\n", 1)[1].splitlines()
    assert {line.split()[0] for line in listed if line.startswith("  ")} == ALLOWED
    assert _paths() == {
        ("whoami",),
        ("token", "set"),
        ("token", "clear"),
        ("apps",),
        ("apps", "create"),
        ("status",),
        ("share",),
        ("unshare",),
        ("doctor",),
        ("init",),
    }
    for group, subs in (("token", {"set", "clear"}), ("apps", {"create"})):
        text = cli(group, "--help").stdout.split("Commands:\n", 1)[1]
        assert {line.split()[0] for line in text.splitlines() if line.startswith("  ")} == subs


def test_every_command_has_a_json_flag():
    root = get_command(app)
    assert isinstance(root, TyperGroup)
    for path in _paths():
        cmd = root.commands[path[0]]
        if len(path) == 2:
            assert isinstance(cmd, TyperGroup)
            cmd = cmd.commands[path[1]]
        assert any("--json" in getattr(p, "opts", ()) for p in cmd.params), path


def test_module_entry_point(tmp_path):
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("SSC_")},
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
    }
    run = [sys.executable, "-m", "ssc_cli"]
    helped = subprocess.run([*run, "--help"], capture_output=True, text=True, env=env, check=True)
    assert helped.stdout.startswith("Usage: ssc ")
    version = subprocess.run(
        [*run, "--version"], capture_output=True, text=True, env=env, check=True
    )
    assert version.stdout == f"ssc {__version__}\n"


# ── refusals and exit codes (fake API) ──────────────────────────────────────


def test_refusal_rendering(cli, fake_api, fake_problem, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    fake_api.add("POST", "/v1/apps", fake_problem(409, "ALREADY_EXISTS"))

    human = cli("apps", "create", "demo", session=fake_api.session())
    assert human.code == ExitCode.FAILED
    assert human.stdout == ""
    assert human.stderr.splitlines() == [
        "Error: title for ALREADY_EXISTS",
        "detail for ALREADY_EXISTS",
        "Code: ALREADY_EXISTS",
        "Request id: req_test",
    ]

    machine = cli("apps", "create", "demo", "--json", session=fake_api.session())
    assert machine.code == ExitCode.FAILED
    assert machine.stderr == ""
    error = ErrorResult.model_validate(machine.json()).error
    assert error.model_dump() == {
        "code": "ALREADY_EXISTS",
        "title": "title for ALREADY_EXISTS",
        "detail": "detail for ALREADY_EXISTS",
        "status": 409,
        "request_id": "req_test",
        "instance": "/v1/x",
        "type": "https://errors.delimitus.com/already_exists",
    }


@pytest.mark.parametrize(
    "args",
    [
        ("share", "demo"),
        ("share", "demo", USR, "--org"),
        ("share", "demo", "bob@example.com"),
        ("share", "demo", APP_ID),
        ("share", "demo", "--org", "--env", "staging"),
        ("share", "demo", "--org", "--role", "owner"),
        ("unshare", "demo", "--org", "--role", "user"),
        ("apps", "create"),
        ("nope",),
    ],
)
def test_usage_errors_exit_2_before_any_request(cli, fake_api, args):
    r = cli(*args, "--json", session=fake_api.session())
    assert r.code == ExitCode.USAGE
    assert r.stdout == ""
    assert "Error:" in r.stderr
    assert fake_api.seen == []


def test_no_token_exits_3(cli, fake_api):
    r = cli("apps", "--json", session=fake_api.session())
    assert r.code == ExitCode.AUTH
    assert r.json()["error"]["code"] == "NO_TOKEN"
    assert fake_api.seen == []


def test_closed_port_exits_5(cli, isolated):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    api = f"http://127.0.0.1:{port}"
    isolated.set_password(SERVICE, api, "tok")
    r = cli("whoami", "--json", session=Session(api_override=api, sleep=_no_sleep))
    assert r.code == ExitCode.NETWORK
    assert r.json()["error"]["code"] == "NETWORK_ERROR"


def test_doctor_blocking_exits_4(cli):
    fixtures = CLEAN.parent
    assert cli("doctor", str(fixtures / "NO_START_COMMAND")).code == ExitCode.BLOCKED
    assert cli("doctor", str(fixtures / "WRITES_HOME")).code == ExitCode.OK


# ── share, unshare and status against a scripted API ────────────────────────


def _app(current_deployment_id: str | None = None) -> dict[str, object]:
    envs = [
        {"id": PREVIEW, "name": "preview", "config_version": 1, "grants_version": 1},
        {
            "id": PROD,
            "name": "prod",
            "config_version": 1,
            "grants_version": 1,
            "current_deployment_id": current_deployment_id,
        },
    ]
    return {
        "id": APP_ID,
        "slug": "demo",
        "owner_user_id": USR,
        "status": "active",
        "created_at": "2026-09-29T00:00:00Z",
        "environments": envs,
    }


def _grants(version: int, *grants: dict[str, object]) -> httpx2.Response:
    body = {"environment_id": PROD, "grants_version": version, "grants": list(grants)}
    return httpx2.Response(200, json=body, headers={"etag": f'"{version}"'})


ORG_USER = {"id": "gnt_1", "role": "user", "subject_kind": "org", "subject_id": None}


@pytest.fixture
def scripted(fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    summary = {k: _app()[k] for k in ("id", "slug", "owner_user_id", "status")}
    fake_api.add("GET", "/v1/apps", httpx2.Response(200, json={"apps": [summary]}))
    fake_api.add("GET", f"/v1/apps/{APP_ID}", httpx2.Response(200, json=_app()))
    return fake_api


def _puts(fake_api) -> list[httpx2.Request]:
    return [r for r in fake_api.seen if r.method == "PUT"]


def test_share_sends_one_if_match_put(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    scripted.add("GET", path, _grants(3))
    scripted.add("PUT", path, _grants(4, ORG_USER))
    r = cli("share", "demo", "--org", "--json", session=scripted.session())
    assert r.code == 0, r.stdout
    result = ShareResult.model_validate(r.json())
    assert (result.changed, result.grants_version, result.environment) == (True, 4, "prod")
    (put,) = _puts(scripted)
    assert put.headers["if-match"] == '"3"'
    assert json.loads(put.content) == {
        "grants": [{"role": "user", "subject_kind": "org", "subject_id": None}]
    }


def test_share_defaults_to_builder_on_preview(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/grants"
    scripted.add("GET", path, _grants(1))
    scripted.add("PUT", path, _grants(2))
    cli("share", APP_ID, USR, "--env", "preview", session=scripted.session())
    (put,) = _puts(scripted)
    assert json.loads(put.content)["grants"] == [
        {"role": "builder", "subject_kind": "user", "subject_id": USR}
    ]


def test_share_changes_the_role_of_an_existing_grant(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    scripted.add("GET", path, _grants(5, ORG_USER))
    scripted.add("PUT", path, _grants(6))
    cli("share", "demo", "--org", "--role", "builder", session=scripted.session())
    (put,) = _puts(scripted)
    assert json.loads(put.content)["grants"] == [
        {"role": "builder", "subject_kind": "org", "subject_id": None}
    ]


def test_share_no_op_sends_no_put(cli, scripted):
    scripted.add("GET", f"/v1/apps/{APP_ID}/environments/{PROD}/grants", _grants(7, ORG_USER))
    r = cli("share", "demo", "--org", session=scripted.session())
    assert r.code == 0
    assert r.stdout.startswith("Nothing to change.")
    assert _puts(scripted) == []


def test_unshare_absent_subject_is_a_no_op(cli, scripted):
    scripted.add("GET", f"/v1/apps/{APP_ID}/environments/{PROD}/grants", _grants(7, ORG_USER))
    r = cli("unshare", "demo", USR, "--json", session=scripted.session())
    assert r.code == 0
    assert r.json()["changed"] is False
    assert _puts(scripted) == []


def test_share_stops_on_other_refusals(cli, scripted, fake_problem):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    scripted.add("GET", path, _grants(1))
    scripted.add("PUT", path, fake_problem(422, "REFERENCE_NOT_FOUND"))
    r = cli("share", "demo", USR, "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "REFERENCE_NOT_FOUND"
    assert len(_puts(scripted)) == 1


def test_unknown_app_and_environment(cli, scripted):
    r = cli("status", "missing", "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "APP_NOT_FOUND"


def test_missing_environment(cli, fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    only_prod = {
        **_app(),
        "environments": [e for e in _app()["environments"] if e["name"] == "prod"],
    }
    fake_api.add("GET", f"/v1/apps/{APP_ID}", httpx2.Response(200, json=only_prod))
    r = cli("share", APP_ID, "--org", "--env", "preview", "--json", session=fake_api.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "ENVIRONMENT_NOT_FOUND"


def test_apps_human_output(cli, fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    fake_api.add("GET", "/v1/apps", httpx2.Response(200, json={"apps": []}))
    fake_api.add("POST", "/v1/apps", httpx2.Response(201, json=_app()))
    assert "No apps yet." in cli("apps", session=fake_api.session()).stdout
    created = cli("apps", "create", "demo", session=fake_api.session())
    assert created.stdout == f"Created demo ({APP_ID}) with environments preview, prod.\n"


def test_status_reads_the_current_deployment(cli, fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    fake_api.add(
        "GET", f"/v1/apps/{APP_ID}", httpx2.Response(200, json=_app("dep_aaaaaaaaaaaaaaaaaaaa"))
    )
    operation = {
        "operation_id": "dep_aaaaaaaaaaaaaaaaaaaa",
        "kind": "deploy",
        "state": "healthy",
        "app_id": APP_ID,
        "environment_id": PROD,
        "release_id": "rel_aaaaaaaaaaaaaaaaaaaa",
        "started_at": "2026-09-29T00:00:00Z",
        "finished_at": "2026-09-29T00:01:00Z",
    }
    fake_api.add(
        "GET", "/v1/operations/dep_aaaaaaaaaaaaaaaaaaaa", httpx2.Response(200, json=operation)
    )
    r = cli("status", APP_ID, "--json", session=fake_api.session())
    assert r.code == 0, r.stdout
    envs = {e.name: e for e in AppResult.model_validate(r.json()).environments}
    assert envs["preview"].deployment is None
    assert envs["prod"].deployment is not None
    assert envs["prod"].deployment.state == "healthy"
    human = cli("status", APP_ID, session=fake_api.session())
    assert "healthy" in human.stdout
    assert "rel_aaaaaaaaaaaaaaaaaaaa" in human.stdout


# ── against the live API ────────────────────────────────────────────────────


@pytest.fixture
def on_live(cli, live, isolated):
    isolated.set_password(SERVICE, live.url, live.token())

    def run(*args: str, input: str | None = None, transport: httpx2.BaseTransport | None = None):
        session = Session(api_override=live.url, transport=transport, sleep=_no_sleep)
        return cli(*args, input=input, session=session)

    return run


def test_every_command_has_json(on_live, live, tmp_path):
    name = slug()
    cases: dict[tuple[str, ...], tuple[list[str], type[BaseModel], str | None]] = {
        ("token", "set"): ([], TokenSetResult, live.token()),
        ("whoami",): ([], WhoamiResult, None),
        ("apps", "create"): ([name], AppResult, None),
        ("apps",): ([], AppsResult, None),
        ("status",): ([name], AppResult, None),
        ("share",): ([name, "--org"], ShareResult, None),
        ("unshare",): ([name, "--org"], ShareResult, None),
        ("doctor",): ([str(CLEAN)], DoctorResult, None),
        ("init",): ([str(tmp_path)], InitResult, None),
        ("token", "clear"): ([], TokenClearResult, None),
    }
    assert set(cases) == _paths()
    for path, (args, shape, stdin) in cases.items():
        r = on_live(*path, *args, "--json", input=stdin)
        assert r.code == 0, (path, r.stdout, r.stderr)
        shape.model_validate(json.loads(r.stdout))


def test_live_exit_codes(on_live, live, isolated):
    name = slug()
    assert on_live("apps", "create", name).code == 0
    again = on_live("apps", "create", name, "--json")
    assert again.code == ExitCode.FAILED
    assert again.json()["error"]["code"] == "ALREADY_EXISTS"
    assert again.json()["error"]["request_id"]

    for bad in ("not.a.token", live.token(ttl=-60)):
        isolated.set_password(SERVICE, live.url, bad)
        refused = on_live("whoami", "--json")
        assert refused.code == ExitCode.AUTH
        assert refused.json()["error"]["status"] == 401
        stored = on_live("token", "set", "--json", input=live.token(ttl=-60))
        assert stored.code == ExitCode.AUTH
        assert isolated.get_password(SERVICE, live.url) == bad


class Racing(httpx2.BaseTransport):
    """Lets another writer change the sharing rules just before each of the next ``n`` PUTs."""

    def __init__(self, url: str, token: str, n: int) -> None:
        self.inner = httpx2.HTTPTransport()
        self.other = httpx2.Client(base_url=url, headers={"authorization": f"Bearer {token}"})
        self.left = n
        self.puts = 0

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "PUT":
            self.puts += 1
            if self.left > 0:
                self.left -= 1
                self._write_first(request.url.path)
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()
        self.other.close()

    def _write_first(self, path: str) -> None:
        current = self.other.get(path)
        grants = [
            {k: g[k] for k in ("role", "subject_kind", "subject_id")}
            for g in current.json()["grants"]
        ]
        if not any(g["subject_kind"] == "user" for g in grants):
            admin = self.other.get("/v1/whoami").json()["subject"]
            grants.append({"role": "builder", "subject_kind": "user", "subject_id": admin})
        r = self.other.put(
            path, json={"grants": grants}, headers={"if-match": current.headers["etag"]}
        )
        assert r.status_code == 200, r.text


def test_share_retries_on_stale_etag(on_live, live):
    name = slug()
    assert on_live("apps", "create", name).code == 0
    racing = Racing(live.url, live.token(), n=2)
    r = on_live("share", name, "--org", "--json", transport=racing)
    assert r.code == 0, r.stdout
    assert racing.puts == 3
    result = ShareResult.model_validate(r.json())
    kept = {(g.role, g.subject_kind, g.subject_id) for g in result.grants}
    assert kept == {("user", "org", None), ("builder", "user", live.admin_id)}


def test_share_gives_up_after_3(on_live, live):
    name = slug()
    assert on_live("apps", "create", name).code == 0
    racing = Racing(live.url, live.token(), n=10)
    r = on_live("share", name, "--org", "--json", transport=racing)
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "PRECONDITION_STALE"
    assert racing.puts == 4
