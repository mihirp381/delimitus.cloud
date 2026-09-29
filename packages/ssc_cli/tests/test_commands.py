"""Every command: the registered set, ``--json``, refusals, exit codes and ``share``'s retries."""

import json
import os
import socket
import subprocess
import sys
import time
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
    DeployResult,
    DoctorResult,
    ErrorResult,
    InitResult,
    ReleasesResult,
    RollbackResult,
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
ALLOWED = {
    "whoami",
    "token",
    "apps",
    "status",
    "share",
    "unshare",
    "doctor",
    "init",
    "deploy",
    "releases",
    "rollback",
    "mcp",
}


def slug() -> str:
    return f"t{uuid.uuid4().hex[:12]}"


def _no_sleep(_: float) -> None:
    return None


def _short_sleep(_: float) -> None:
    """Polls on the live stack sleep a little for real, so the worker gets to run."""
    time.sleep(0.05)


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
        ("deploy",),
        ("releases",),
        ("rollback",),
        ("mcp",),
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
        ("share", "demo", "bob@"),
        ("share", "demo", "bob smith@example.com"),
        ("share", "demo", "usr_short"),
        ("share", "demo", "grp_AAAAAAAAAAAAAAAAAAAA"),
        ("share", "demo", APP_ID),
        ("unshare", "demo", ""),
        ("unshare", "demo", "g" * 201),
        ("share", "demo", "--org", "--env", "staging"),
        ("share", "demo", "--org", "--role", "owner"),
        ("unshare", "demo", "--org", "--role", "user"),
        ("unshare", "demo"),
        ("unshare", "demo", USR, USR),
        ("share", "demo", "--org", "--group"),
        ("unshare", "demo", "--org", "--group"),
        ("share", "demo", USR, "grp_financefinancefinanc"),
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


APR = "apr_aaaaaaaaaaaaaaaaaaaa"


def test_share_through_an_agent_waits_for_approval(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    pending = {"environment_id": PROD, "grants_version": 3, "approval_ids": [APR]}
    for _ in range(2):
        scripted.add("GET", path, _grants(3, ORG_USER))
        scripted.add("PUT", path, httpx2.Response(202, json=pending, headers={"etag": '"3"'}))
    r = cli("share", "demo", USR, "--json", session=scripted.session())
    assert r.code == 0, r.stdout
    result = ShareResult.model_validate(r.json())
    assert (result.changed, result.grants_version, result.pending) == (False, 3, [APR])
    assert [g.id for g in result.grants] == ["gnt_1"]
    human = cli("share", "demo", USR, session=scripted.session())
    assert human.code == 0
    assert human.stdout.startswith(f"Waiting for approval, nothing changed yet: {APR}.")
    assert len(_puts(scripted)) == 2


def test_applied_share_has_no_pending(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    scripted.add("GET", path, _grants(3))
    scripted.add("PUT", path, _grants(4, ORG_USER))
    r = cli("share", "demo", "--org", "--json", session=scripted.session())
    assert r.json()["pending"] == []


def test_approval_required_says_how_to_ask(cli, scripted, fake_problem):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    for _ in range(2):
        scripted.add("GET", path, _grants(3, ORG_USER))
        scripted.add("PUT", path, fake_problem(409, "APPROVAL_REQUIRED"))
    r = cli("share", "demo", USR, session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert "Code: APPROVAL_REQUIRED" in r.stderr
    (fix,) = [line for line in r.stderr.splitlines() if line.startswith("Fix: ")]
    ask = json.loads(fix.split("POST /v1/approvals ", 1)[1].split(", then", 1)[0])
    assert ask == {
        "environment_id": PROD,
        "kind": "widen_audience",
        "payload": {
            "grants": [
                {"role": "user", "subject_kind": "org", "subject_id": None},
                {"role": "user", "subject_kind": "user", "subject_id": USR},
            ]
        },
    }
    as_json = cli("share", "demo", USR, "--json", session=scripted.session())
    assert as_json.code == ExitCode.FAILED
    assert as_json.json()["error"]["code"] == "APPROVAL_REQUIRED"
    ErrorResult.model_validate(as_json.json())
    assert len(_puts(scripted)) == 2


def _fix(stderr: str) -> str:
    (fix,) = [line for line in stderr.splitlines() if line.startswith("Fix: ")]
    return fix.removeprefix("Fix: ")


OLD = "usr_oldoldoldoldoldoldold"


def test_a_role_below_the_floor_says_which_role(cli, scripted, fake_problem):
    path = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/grants"
    scripted.add("GET", path, _grants(1))
    scripted.add("PUT", path, fake_problem(422, "VALIDATION_FAILED"))
    r = cli("share", "demo", USR, "--env", "preview", "--role", "user", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert "Code: VALIDATION_FAILED" in r.stderr
    assert _fix(r.stderr) == (
        f"preview takes builder grants, so {USR} cannot be a user there; use `--role builder`."
    )


def test_a_stored_grant_below_the_floor_is_named_with_its_removal(cli, scripted, fake_problem):
    path = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/grants"
    legacy = {"id": "gnt_2", "role": "user", "subject_kind": "user", "subject_id": OLD}
    org = {"id": "gnt_3", "role": "user", "subject_kind": "org", "subject_id": None}
    scripted.add("GET", path, _grants(4, legacy, org))
    scripted.add("PUT", path, fake_problem(422, "VALIDATION_FAILED"))
    r = cli("share", "demo", USR, "--env", "preview", session=scripted.session())
    assert r.code == ExitCode.FAILED
    fix = _fix(r.stderr)
    assert f"the user grant for {OLD} was saved before the rule" in fix
    assert f"remove them first with `ssc unshare demo {OLD} --org --env preview`." in fix
    assert fix.count("ssc unshare") == 1
    assert USR not in fix


OLD1 = "usr_oldaoldaoldaoldaolda"
OLD2 = "usr_old2old2old2old2old2"


def test_two_stored_grants_below_the_floor_go_in_one_unshare(cli, scripted, fake_problem):
    path = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/grants"
    legacy = [
        {"id": f"gnt_{i}", "role": "user", "subject_kind": "user", "subject_id": uid}
        for i, uid in enumerate((OLD1, OLD2))
    ]
    kept = {"id": "gnt_9", "role": "builder", "subject_kind": "user", "subject_id": USR}
    scripted.add("GET", path, _grants(4, *legacy, kept))
    scripted.add("PUT", path, fake_problem(422, "VALIDATION_FAILED"))
    r = cli("share", "demo", GRP, "--env", "preview", session=scripted.session())
    fix = _fix(r.stderr)
    command = f"ssc unshare demo {OLD1} {OLD2} --env preview"
    assert f"`{command}`" in fix
    scripted.routes[("PUT", path)] = [_grants(5, kept)]
    scripted.seen.clear()
    done = cli(*command.split()[1:], "--json", session=scripted.session())
    assert done.code == 0, done.stdout
    (put,) = _puts(scripted)
    assert json.loads(put.content)["grants"] == [
        {"role": "builder", "subject_kind": "user", "subject_id": USR}
    ]
    result = ShareResult.model_validate(done.json())
    assert [(x.kind, x.id) for x in result.subjects] == [("user", OLD1), ("user", OLD2)]
    assert (result.subject_kind, result.subject_id, result.changed) == ("user", OLD1, True)


def test_unshares_that_leave_another_stored_grant_name_every_one(cli, scripted, fake_problem):
    path = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/grants"
    legacy = [
        {"id": f"gnt_{i}", "role": "user", "subject_kind": "user", "subject_id": uid}
        for i, uid in enumerate((OLD1, OLD2))
    ]
    kept = {"id": "gnt_9", "role": "builder", "subject_kind": "user", "subject_id": USR}
    for _ in range(3):
        scripted.add("GET", path, _grants(4, *legacy, kept))
        scripted.add("PUT", path, fake_problem(422, "VALIDATION_FAILED"))
    both = f"`ssc unshare demo {OLD1} {OLD2} --env preview`"
    for first in (OLD1, OLD2):
        r = cli("unshare", "demo", first, "--env", "preview", session=scripted.session())
        assert r.code == ExitCode.FAILED
        assert f"remove them first with {both}." in _fix(r.stderr)
    r = cli("unshare", "demo", USR, "--env", "preview", session=scripted.session())
    assert f"`ssc unshare demo {OLD1} {OLD2} {USR} --env preview`" in _fix(r.stderr)


def test_unshare_says_which_subjects_had_no_grant(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    mine = {"id": "gnt_2", "role": "user", "subject_kind": "user", "subject_id": USR}
    scripted.add("GET", path, _grants(3, mine))
    scripted.add("PUT", path, _grants(4))
    r = cli("unshare", "demo", USR, OLD1, "--org", session=scripted.session())
    assert r.code == 0, r.stdout
    assert r.stdout.startswith(
        f"Removed {USR} from prod; {OLD1}, everyone in the org had no grant.\n"
    )


def test_unshare_takes_ids_and_the_org_in_one_put(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    mine = {"id": "gnt_2", "role": "user", "subject_kind": "user", "subject_id": USR}
    scripted.add("GET", path, _grants(3, ORG_USER, mine))
    scripted.add("PUT", path, _grants(4))
    r = cli("unshare", "demo", USR, "--org", session=scripted.session())
    assert r.code == 0, r.stdout
    assert r.stdout.startswith(f"Removed {USR}, everyone in the org from prod.")
    (put,) = _puts(scripted)
    assert json.loads(put.content)["grants"] == []


def test_group_reads_a_name_with_an_at_or_an_id_prefix_as_a_group(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/grants"
    for name in ("ops@lists", "usr_team"):
        scripted.seen.clear()
        scripted.routes[("GET", "/v1/groups")] = [_groups(_group(GRP, name))]
        scripted.routes[("GET", path)] = [_grants(1)]
        scripted.routes[("PUT", path)] = [_grants(2)]
        r = cli(
            "share",
            "demo",
            name,
            "--group",
            "--env",
            "preview",
            "--json",
            session=scripted.session(),
        )
        assert r.code == 0, r.stdout
        (lookup,) = _lookups(scripted)
        assert (lookup.url.path, lookup.url.params["name"]) == ("/v1/groups", name)
        assert (r.json()["subject_kind"], r.json()["subject_id"]) == ("group", GRP)


def test_a_refusal_the_rules_do_not_explain_gets_the_rules(cli, scripted, fake_problem):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    scripted.add("GET", path, _grants(1))
    scripted.add("PUT", path, fake_problem(422, "VALIDATION_FAILED"))
    r = cli("share", "demo", USR, session=scripted.session())
    assert _fix(r.stderr).startswith("prod takes user or builder grants, preview takes builder")


def test_forbidden_says_who_may_change_that_environment(cli, scripted, fake_problem):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    scripted.add("GET", path, _grants(1))
    scripted.add("PUT", path, fake_problem(403, "FORBIDDEN"))
    r = cli("share", "demo", "--org", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert "Code: FORBIDDEN" in r.stderr
    assert "Request id: req_test" in r.stderr
    assert _fix(r.stderr).startswith(
        "only an org admin, the app's owner or a builder on prod can change who uses prod"
    )
    as_json = cli("share", "demo", "--org", "--json", session=scripted.session())
    assert as_json.json()["error"]["code"] == "FORBIDDEN"
    ErrorResult.model_validate(as_json.json())


# ── naming a person by email or a group by name ─────────────────────────────

BOB = "usr_bobbobbobbobbobbobbo"
BOB2 = "usr_bob2bob2bob2bob2bob2"
GRP = "grp_financefinancefinanc"
GRP2 = "grp_finance2finance2fina"


def _person(uid: str, status: str = "active") -> dict[str, str]:
    return {
        "id": uid,
        "display_name": f"Bob {uid[-3:]}",
        "email": "bob+ops@example.com",
        "role": "member",
        "status": status,
    }


def _users(*people: dict[str, str]) -> httpx2.Response:
    return httpx2.Response(200, json={"users": list(people)})


def _group(gid: str, name: str = "Finance Team", members: int = 3) -> dict[str, object]:
    return {"id": gid, "name": name, "member_count": members}


def _groups(*groups: dict[str, object]) -> httpx2.Response:
    return httpx2.Response(200, json={"groups": list(groups)})


def _lookups(fake_api) -> list[httpx2.Request]:
    return [r for r in fake_api.seen if r.url.path in {"/v1/users", "/v1/groups"}]


def _grant_calls(fake_api) -> list[httpx2.Request]:
    return [r for r in fake_api.seen if r.url.path.endswith("/grants")]


def test_share_by_email_grants_the_one_active_person(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    scripted.add("GET", "/v1/users", _users(_person(BOB2, "deactivated"), _person(BOB)))
    scripted.add("GET", path, _grants(1))
    scripted.add("PUT", path, _grants(2))
    r = cli("share", "demo", "Bob+Ops@Example.com", "--json", session=scripted.session())
    assert r.code == 0, r.stdout
    result = ShareResult.model_validate(r.json())
    assert (result.subject_kind, result.subject_id, result.changed) == ("user", BOB, True)
    (lookup,) = _lookups(scripted)
    assert lookup.url.params["email"] == "Bob+Ops@Example.com"
    assert "%2B" in str(lookup.url)
    (put,) = _puts(scripted)
    assert json.loads(put.content)["grants"] == [
        {"role": "user", "subject_kind": "user", "subject_id": BOB}
    ]
    human = cli("share", "demo", "Bob+Ops@Example.com", session=scripted.session())
    assert human.stdout.startswith(f"Shared prod with Bob+Ops@Example.com ({BOB}) as user.")


def test_share_by_id_or_org_looks_nothing_up(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    scripted.add("GET", path, _grants(1))
    scripted.add("PUT", path, _grants(2))
    r = cli("share", "demo", USR, "--json", session=scripted.session())
    assert (r.json()["subject_kind"], r.json()["subject_id"]) == ("user", USR)
    r = cli("share", "demo", "--org", "--json", session=scripted.session())
    assert (r.json()["subject_kind"], r.json()["subject_id"]) == ("org", None)
    assert _lookups(scripted) == []


def test_an_address_several_people_share_names_each_of_them(cli, scripted):
    scripted.add("GET", "/v1/users", _users(_person(BOB), _person(BOB2)))
    r = cli("share", "demo", "bob+ops@example.com", "--json", session=scripted.session())
    assert r.code == ExitCode.USAGE
    error = r.json()["error"]
    assert (error["code"], error["status"]) == ("SUBJECT_AMBIGUOUS", None)
    assert BOB in error["detail"]
    assert BOB2 in error["detail"]
    assert _grant_calls(scripted) == []
    human = cli("share", "demo", "bob+ops@example.com", session=scripted.session())
    assert _fix(human.stderr) == "run the command again with the usr_ id you mean."


def test_share_passes_over_deactivated_people_and_unshare_does_not(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PROD}/grants"
    held = {"id": "gnt_b", "role": "user", "subject_kind": "user", "subject_id": BOB}
    scripted.add("GET", "/v1/users", _users(_person(BOB, "deactivated")))
    scripted.add("GET", path, _grants(4, held))
    scripted.add("PUT", path, _grants(5))
    r = cli("share", "demo", "bob+ops@example.com", "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    error = r.json()["error"]
    assert (error["code"], error["status"]) == ("USER_NOT_FOUND", None)
    assert BOB in error["detail"]
    assert _grant_calls(scripted) == []
    gone = cli("unshare", "demo", "bob+ops@example.com", "--json", session=scripted.session())
    assert gone.code == 0, gone.stdout
    assert (gone.json()["changed"], gone.json()["subject_id"]) == (True, BOB)
    (put,) = _puts(scripted)
    assert json.loads(put.content) == {"grants": []}


def test_an_unknown_address_is_user_not_found(cli, scripted):
    scripted.add("GET", "/v1/users", _users())
    r = cli("unshare", "demo", "nobody@example.com", "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "USER_NOT_FOUND"
    assert "nobody@example.com" in r.json()["error"]["detail"]
    assert _grant_calls(scripted) == []


def test_a_builder_sharing_by_email_is_told_to_use_the_usr_id(cli, scripted, fake_problem):
    scripted.add("GET", "/v1/users", fake_problem(403, "FORBIDDEN"))
    r = cli("share", "demo", "bob@example.com", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert "Code: FORBIDDEN" in r.stderr
    assert _fix(r.stderr).startswith(
        "only an org admin can look people up by email, so a builder shares by usr_ id"
    )
    as_json = cli("share", "demo", "bob@example.com", "--json", session=scripted.session())
    assert (as_json.json()["error"]["code"], as_json.json()["error"]["status"]) == (
        "FORBIDDEN",
        403,
    )
    assert _grant_calls(scripted) == []


def test_share_by_group_name(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/grants"
    scripted.add("GET", "/v1/groups", _groups(_group(GRP)))
    scripted.add("GET", path, _grants(1))
    scripted.add("PUT", path, _grants(2))
    args = ("share", "demo", "finance team", "--env", "preview")
    r = cli(*args, "--json", session=scripted.session())
    assert r.code == 0, r.stdout
    assert (r.json()["subject_kind"], r.json()["subject_id"]) == ("group", GRP)
    (lookup,) = _lookups(scripted)
    assert (lookup.url.path, lookup.url.params["name"]) == ("/v1/groups", "finance team")
    (put,) = _puts(scripted)
    assert json.loads(put.content)["grants"] == [
        {"role": "builder", "subject_kind": "group", "subject_id": GRP}
    ]
    human = cli(*args, session=scripted.session())
    assert human.stdout.startswith(f"Shared preview with group Finance Team ({GRP}) as builder.")


def test_a_group_name_two_groups_share_names_both(cli, scripted):
    scripted.add("GET", "/v1/groups", _groups(_group(GRP), _group(GRP2, "FINANCE TEAM", 0)))
    r = cli("unshare", "demo", "Finance Team", "--json", session=scripted.session())
    assert r.code == ExitCode.USAGE
    error = r.json()["error"]
    assert error["code"] == "SUBJECT_AMBIGUOUS"
    assert f"{GRP} (Finance Team, 3 active members)" in error["detail"]
    assert f"{GRP2} (FINANCE TEAM, 0 active members)" in error["detail"]
    assert _grant_calls(scripted) == []


def test_an_unknown_group_name_is_group_not_found(cli, scripted):
    scripted.add("GET", "/v1/groups", _groups())
    r = cli("share", "demo", "Finance", "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert (r.json()["error"]["code"], r.json()["error"]["status"]) == ("GROUP_NOT_FOUND", None)
    assert _grant_calls(scripted) == []


def test_a_group_lookup_refused_says_who_may_share(cli, scripted, fake_problem):
    scripted.add("GET", "/v1/groups", fake_problem(403, "FORBIDDEN"))
    r = cli("share", "demo", "Finance", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert _fix(r.stderr).startswith(
        "only an org admin, the app's owner or a builder on prod can change who uses prod"
    )


def test_whoami_shows_the_org_role(cli, fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    me = {
        "org_id": ORG,
        "subject": USR,
        "kind": "user",
        "credential_id": "cred_1",
        "is_agent": False,
        "client_id": None,
    }
    fake_api.add(
        "GET",
        "/v1/whoami",
        httpx2.Response(200, json={**me, "role": "admin"}),
        httpx2.Response(200, json={**me, "role": None}),
    )
    r = cli("whoami", "--json", session=fake_api.session())
    assert WhoamiResult.model_validate(r.json()).role == "admin"
    human = cli("whoami", session=fake_api.session())
    assert [line.split() for line in human.stdout.splitlines() if line.startswith("role")] == [
        ["role", "-"]
    ]


def test_the_floor_is_the_apis(cli):
    from ssc_cli.commands.share import FLOOR
    from ssc_control.domain.grant_rules import FLOOR as API_FLOOR

    assert {e.value: r.value for e, r in FLOOR.items()} == dict(API_FLOOR)


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
        session = Session(api_override=live.url, transport=transport, sleep=_short_sleep)
        return cli(*args, input=input, session=session)

    return run


def test_every_command_has_json(on_live, live, tmp_path):
    name = slug()
    folder = tmp_path / "app"
    folder.mkdir()
    (folder / "ssc.toml").write_text('schema = "ssc/v1"\n')
    (folder / "main.py").write_text("print('hello')\n")
    cases: dict[tuple[str, ...], tuple[list[str], type[BaseModel], str | None]] = {
        ("token", "set"): ([], TokenSetResult, live.token()),
        ("whoami",): ([], WhoamiResult, None),
        ("apps", "create"): ([name], AppResult, None),
        ("apps",): ([], AppsResult, None),
        ("status",): ([name], AppResult, None),
        ("share",): ([name, "--org"], ShareResult, None),
        ("unshare",): ([name, "--org"], ShareResult, None),
        ("deploy",): ([str(folder), "--app", name, "--wait"], DeployResult, None),
        ("releases",): ([name], ReleasesResult, None),
        ("rollback",): ([name, "R1", "--wait"], RollbackResult, None),
        ("doctor",): ([str(CLEAN)], DoctorResult, None),
        ("init",): ([str(tmp_path)], InitResult, None),
        ("token", "clear"): ([], TokenClearResult, None),
    }
    # `mcp` serves stdio; its --json covers start-up refusals only (test_mcp_local.py).
    assert set(cases) | {("mcp",)} == _paths()
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


def test_live_share_through_an_agent_waits_for_approval(on_live, live, isolated):
    name = slug()
    assert on_live("apps", "create", name).code == 0
    isolated.set_password(SERVICE, live.url, live.token(agent=True))
    r = on_live("share", name, "--org", "--json")
    assert r.code == 0, r.stdout
    result = ShareResult.model_validate(r.json())
    assert result.changed is False
    assert result.grants == []
    assert result.pending
    assert all(i.startswith("apr_") for i in result.pending)
    again = on_live("share", name, "--org")
    assert again.code == 0, again.stderr
    assert again.stdout.startswith(
        f"Waiting for approval, nothing changed yet: {result.pending[0]}"
    )
    isolated.set_password(SERVICE, live.url, live.token())
    after = on_live("status", name, "--json").json()
    prod = next(e for e in after["environments"] if e["name"] == "prod")
    assert prod["grants_version"] == result.grants_version


def test_live_floor_refusal_is_explained(on_live, live):
    name = slug()
    assert on_live("apps", "create", name).code == 0
    r = on_live("share", name, live.admin_id, "--env", "preview", "--role", "user")
    assert r.code == ExitCode.FAILED
    assert "Code: VALIDATION_FAILED" in r.stderr
    assert _fix(r.stderr).endswith("use `--role builder`.")
    ok = on_live("share", name, live.admin_id, "--env", "preview", "--json")
    assert ok.code == 0, ok.stdout
    assert [g["role"] for g in ok.json()["grants"]] == ["builder"]


def test_live_owner_may_remove_every_grant(on_live, live):
    name = slug()
    created = on_live("apps", "create", name, "--json").json()
    assert created["owner_user_id"] == live.admin_id
    assert on_live("share", name, live.admin_id, "--json").code == 0
    gone = on_live("unshare", name, live.admin_id, "--json")
    assert gone.code == 0, gone.stdout
    assert (gone.json()["changed"], gone.json()["grants"]) == (True, [])


def _operator(live) -> httpx2.Client:
    from ssc_control.api.settings import INTERNAL_AUDIENCE

    token = live.token(kind="operator", sub="op_sync", audience=INTERNAL_AUDIENCE)
    return httpx2.Client(base_url=live.url, headers={"authorization": f"Bearer {token}"})


def test_live_share_by_email_and_group_name(on_live, live, isolated):
    name = slug()
    assert on_live("apps", "create", name).code == 0
    by_email = on_live("share", name, "DEV@example.invalid", "--json")
    assert by_email.code == 0, by_email.stdout
    assert (by_email.json()["subject_id"], by_email.json()["changed"]) == (live.admin_id, True)

    team = f"Finance Team {uuid.uuid4().hex[:8]}"
    key = {"idempotency-key": str(uuid.uuid4())}
    with _operator(live) as op:
        made = op.post(
            "/internal/v1/directory/groups",
            json={"directory_ref": f"ref-{team}", "display_name": team},
            headers=key,
        )
        assert made.status_code == 200, made.text
        group = made.json()["group_id"]
        members = op.put(
            f"/internal/v1/directory/groups/{group}/members", json={"user_ids": [live.admin_id]}
        )
        assert members.status_code == 200, members.text
    by_name = on_live("share", name, team.upper(), "--env", "preview", "--json")
    assert by_name.code == 0, by_name.stdout
    assert by_name.json()["subject_id"] == group
    assert {"role": "builder", "subject_kind": "group", "subject_id": group} in [
        {k: g[k] for k in ("role", "subject_kind", "subject_id")} for g in by_name.json()["grants"]
    ]


def test_live_a_member_sharing_by_email_is_told_to_use_the_usr_id(on_live, live, isolated):
    subject = f"sub-{uuid.uuid4().hex}"
    person = {
        "issuer": "https://dev.invalid",
        "subject": subject,
        "display_name": "Mia Member",
        "email": f"{subject}@example.com",
        "role": "member",
        "status": "active",
    }
    with _operator(live) as op:
        made = op.post(
            "/internal/v1/directory/users",
            json=person,
            headers={"idempotency-key": str(uuid.uuid4())},
        )
        assert made.status_code == 200, made.text
    isolated.set_password(SERVICE, live.url, live.token(sub=made.json()["user_id"]))
    name = slug()
    assert on_live("apps", "create", name).code == 0
    r = on_live("share", name, "dev@example.invalid")
    assert r.code == ExitCode.FAILED
    assert "Code: FORBIDDEN" in r.stderr
    assert "shares by usr_ id" in _fix(r.stderr)
    me = on_live("whoami", "--json").json()
    assert me["role"] == "member"


def test_live_widening_a_data_connected_app_says_how_to_ask(on_live, live):
    name = slug()
    created = on_live("apps", "create", name, "--json").json()
    prod = next(e["id"] for e in created["environments"] if e["name"] == "prod")
    headers = {"authorization": f"Bearer {live.token()}"}
    with httpx2.Client(base_url=live.url, headers=headers) as http:
        connect = {"environment_id": prod, "kind": "connect_data_source", "subject_key": "finance"}
        asked = http.post(
            "/v1/approvals", json=connect, headers={"idempotency-key": str(uuid.uuid4())}
        )
        assert asked.status_code == 201, asked.text
        r = on_live("share", name, "--org")
        assert r.code == ExitCode.FAILED
        assert "Code: APPROVAL_REQUIRED" in r.stderr
        (fix,) = [line for line in r.stderr.splitlines() if line.startswith("Fix: ")]
        ask = json.loads(fix.split("POST /v1/approvals ", 1)[1].split(", then", 1)[0])
        widen = http.post("/v1/approvals", json=ask, headers={"idempotency-key": str(uuid.uuid4())})
        assert widen.status_code == 201, widen.text
        assert widen.json()["kind"] == "widen_audience"
