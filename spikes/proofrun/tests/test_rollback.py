import argparse
import json
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from proofrun import rollback as rb
from proofrun.__main__ import PROOFS, parser
from proofrun.common import KIT, Done, FencedError, emit

TOKEN = "operator-token-value"
SLUG = "rollbackapp"
OP = "dep_" + "a" * 20
APP_ID, ENV_ID = "app_" + "b" * 20, "env_" + "c" * 20
R1_ID, R2_ID = "rel_" + "1" * 20, "rel_" + "2" * 20
AFTER = {
    "environment_id": ENV_ID,
    "release_id": R1_ID,
    "migrations_ahead": [rb.LEDGER_ENTRY],
    "confirmed": True,
}
ROW = {
    "seq": 7,
    "at": "2026-10-08T12:00:00Z",
    "action": "rollback.started",
    "actor": {"kind": "user", "id": "usr_x", "via_agent": False, "client_id": None},
    "target": {"kind": "deployment", "id": OP},
    "after": AFTER,
}
FIX = f"Fix: The preview database may have run migrations R5 does not have (alembic: {rb.AHEAD})."
REFUSED_TEXT = Done(4, "", f"Error: The database has migrations.\nCode: SCHEMA_AHEAD\n{FIX}\n")
REFUSED_JSON = Done(
    4, json.dumps({"error": {"code": "SCHEMA_AHEAD", "title": "t", "detail": "d"}}), ""
)


def sent(number: int, release_id: str, state: str = "healthy") -> Done:
    body = {
        "app_id": APP_ID,
        "environment_id": ENV_ID,
        "release_number": number,
        "release_id": release_id,
        "operation_id": OP,
        "state": state,
    }
    return Done(0, json.dumps(body), "")


def mcp_reply(error: bool = True, code: str = "SCHEMA_AHEAD", names: bool = True) -> dict[str, Any]:
    detail = f"fixed. The database may have run: alembic {rb.AHEAD if names else 'none listed'}."
    text = f"{code}: title {detail}"
    return {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "isError": error,
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"error": {"code": code, "detail": detail}},
        },
    }


class FakeMcp:
    """A scripted ``ssc mcp``: what it was sent and what it answers."""

    def __init__(self, replies: Mapping[str, dict[str, Any] | None], stderr: str = "") -> None:
        self.replies = replies
        self.stderr = stderr
        self.messages: list[tuple[str, Any]] = []
        self.closed = False

    def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
        self.messages.append((method, params))
        return self.replies.get(method)

    def notify(self, method: str) -> None:
        self.messages.append((method, None))

    def close(self) -> str:
        self.closed = True
        return self.stderr


def good_mcp() -> FakeMcp:
    return FakeMcp(
        {
            "initialize": {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "x"}},
            "tools/list": {
                "id": 2,
                "result": {"tools": [{"name": "deploy"}, {"name": "rollback"}]},
            },
            "tools/call": mcp_reply(),
        }
    )


class World:
    """The ssc commands the kit runs, answered like a healthy platform, and what it was asked."""

    def __init__(self, **over: Done) -> None:
        self.over = over
        self.calls: list[list[str]] = []
        self.deploys = 0
        self.ahead_file_present: list[bool] = []
        self.deploy_dirs: list[Path] = []
        self.rows: list[dict[str, Any]] = [ROW]

    def __call__(self, argv: Sequence[str], *, cwd: Path | None = None, env: Any = None) -> Done:
        self.calls.append(list(argv))
        words = list(argv)
        if "deploy" in words:
            return self.deploy(words)
        if "audit" in words:
            return self.audit(words)
        assert "rollback" in words, argv
        if "--confirm" in words:
            return self.over.get("confirm", sent(5, R1_ID))
        if "--wait" in words:
            return self.over.get("forward", sent(6, R2_ID))
        if "--json" in words:
            return self.over.get("refuse_json", REFUSED_JSON)
        return self.over.get("refuse_text", REFUSED_TEXT)

    def deploy(self, words: list[str]) -> Done:
        self.deploys += 1
        folder = Path(words[words.index(self.slug_arg(words)) + 1])
        self.deploy_dirs.append(folder)
        self.ahead_file_present.append((folder / rb.AHEAD_FILE).exists())
        key = "deploy1" if self.deploys == 1 else "deploy2"
        default = sent(5, R1_ID) if self.deploys == 1 else sent(6, R2_ID)
        return self.over.get(key, default)

    @staticmethod
    def slug_arg(words: list[str]) -> str:
        return words[words.index("--app") + 1]

    def audit(self, words: list[str]) -> Done:
        if "audit_fail" in self.over:
            return self.over["audit_fail"]
        out = Path(words[words.index("--out") + 1])
        out.write_text("".join(json.dumps(r) + "\n" for r in self.rows) + "not json\n")
        return Done(0, "{}", "")


def drive(
    world: World, mcp: FakeMcp | None = None, console: str = rb.CONSOLE_URL
) -> tuple[Any, list[str], FakeMcp]:
    said: list[str] = []
    mcp = mcp or good_mcp()
    args = argparse.Namespace(app=SLUG, console_url=console)
    outcome = rb.run(args, run=world, open_mcp=lambda argv: mcp, say=said.append)
    return outcome, said, mcp


def by_n(outcome: Any) -> dict[int, dict[str, Any]]:
    return {c["n"]: c for c in outcome.data["checks"]}


def test_a_healthy_platform_passes_every_automatic_check() -> None:
    world = World()
    outcome, _, _ = drive(world)
    checks = by_n(outcome)
    assert outcome.passed is True
    assert [checks[n]["result"] for n in range(1, 10)] == ["PASS"] * 9
    assert checks[10]["result"] == "manual"
    assert outcome.final_line().endswith("PASS")
    assert "9 of 9 automatic checks passed" in outcome.number
    assert outcome.proof == "GA-4.5"


def test_the_order_of_ssc_commands_and_the_real_steps() -> None:
    world = World()
    outcome, said, _ = drive(world)
    kinds = []
    for call in world.calls:
        word = next(w for w in call if w in ("deploy", "rollback", "audit"))
        kinds.append(word + (" confirm" if "--confirm" in call else ""))
    assert kinds == [
        "deploy",
        "deploy",
        "rollback",
        "rollback",
        "rollback confirm",
        "audit",
        "rollback",
    ]
    assert said[0].startswith(f"app {SLUG}: preview only")
    assert sum("[real]" in s for s in said) == 5  # the heading and the four steps
    assert outcome.data["plan"] == said


def test_r1_comes_from_a_temporary_copy_without_the_second_migration() -> None:
    world = World()
    drive(world)
    assert world.ahead_file_present == [False, True]
    assert world.deploy_dirs[0] != rb.APP_FOLDER
    assert world.deploy_dirs[1] == rb.APP_FOLDER
    assert not world.deploy_dirs[0].exists()
    assert (rb.APP_FOLDER / rb.AHEAD_FILE).exists()
    assert "--wait" in world.calls[0]
    assert str(rb.FIRST_TIMEOUT_S) in world.calls[0]


def test_every_ssc_call_is_at_the_repo_root_with_the_ssc_prefix() -> None:
    seen: list[Path | None] = []

    class Recorder(World):
        def __call__(
            self, argv: Sequence[str], *, cwd: Path | None = None, env: Any = None
        ) -> Done:
            seen.append(cwd)
            assert list(argv[:3]) == ["uv", "run", "ssc"]
            return super().__call__(argv, cwd=cwd, env=env)

    drive(Recorder())
    assert seen and all(c is not None and c == rb.REPO for c in seen)


def test_the_refusal_texts_and_the_json_note() -> None:
    outcome, _, _ = drive(World())
    assert rb.NOTE_JSON in outcome.lines
    assert rb.NOTE_JSON == (
        "note: names absent from --json (CliError.fix is text-only, errors.py:104-106)"
    )
    assert outcome.data["notes"] == [rb.NOTE_JSON]
    assert outcome.data["cli_json_names"] is False


def test_names_in_json_would_change_the_note() -> None:
    named = Done(4, json.dumps({"error": {"code": "SCHEMA_AHEAD", "detail": f"x {rb.AHEAD}"}}), "")
    outcome, _, _ = drive(World(refuse_json=named))
    assert rb.NOTE_JSON not in outcome.lines
    assert outcome.data["cli_json_names"] is True


def test_check_deployed() -> None:
    assert rb.check_deployed(1, sent(5, R1_ID)).result is True
    assert rb.check_deployed(1, sent(5, R1_ID, "failed")).result is False
    assert rb.check_deployed(1, Done(1, "", "boom")).result is False
    assert rb.check_deployed(2, sent(6, R2_ID), above=5).result is True
    assert rb.check_deployed(2, sent(5, R2_ID), above=5).result is False


def test_check_cli_text() -> None:
    assert rb.check_cli_text(REFUSED_TEXT).result is True
    assert rb.check_cli_text(Done(0, "", FIX + " SCHEMA_AHEAD")).result is False
    assert rb.check_cli_text(Done(4, "", "Code: SCHEMA_AHEAD\n")).result is False
    assert rb.check_cli_text(Done(4, "", f"Code: OTHER\n{rb.AHEAD}")).result is False
    both = Done(4, "", f"Code: SCHEMA_AHEAD\n{rb.AHEAD} {rb.FIRST}")
    assert rb.check_cli_text(both).result is False


def test_check_cli_json() -> None:
    assert rb.check_cli_json(REFUSED_JSON).result is True
    assert rb.check_cli_json(Done(0, REFUSED_JSON.stdout, "")).result is False
    other = Done(4, json.dumps({"error": {"code": "NOT_FOUND"}}), "")
    assert rb.check_cli_json(other).result is False
    assert rb.check_cli_json(Done(4, "not json", "")).result is False


@pytest.mark.parametrize(
    ("reply", "want"),
    [
        (mcp_reply(), True),
        (mcp_reply(error=False), False),
        (mcp_reply(code="NOT_FOUND"), False),
        (mcp_reply(names=False), False),
        ({"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": "bad"}}, False),
    ],
)
def test_check_mcp_results(reply: dict[str, Any], want: bool) -> None:
    found = rb.McpFindings(True, ["rollback"], reply)
    assert rb.check_mcp(found).result is want


def test_check_mcp_without_the_tool_or_answers() -> None:
    assert rb.check_mcp(rb.McpFindings(True, ["deploy"], mcp_reply())).result is False
    assert rb.check_mcp(rb.McpFindings(True, None, None)).result is None
    assert rb.check_mcp(rb.McpFindings(True, ["rollback"], None)).result is None


def test_check_mcp_needing_an_agent_login_is_not_read_and_says_how() -> None:
    err = "Error: ssc mcp needs an agent's token.\nCode: AGENT_TOKEN_REQUIRED\n"
    check = rb.check_mcp(rb.McpFindings(False, None, None, err))
    assert check.result is None
    assert "ssc login --org <org id> --agent ga45" in check.detail
    assert "SSC_TOKEN" in check.detail
    other = rb.check_mcp(rb.McpFindings(False, None, None, "Traceback\nValueError: x\n"))
    assert other.result is None
    assert "ValueError: x" in other.detail


def test_the_mcp_conversation_is_initialize_initialized_list_call() -> None:
    mcp = good_mcp()
    found = rb.mcp_conversation(lambda argv: mcp, SLUG, R1_ID)
    methods = [m for m, _ in mcp.messages]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    call = mcp.messages[3][1]
    assert call == {
        "name": "rollback",
        "arguments": {"app": SLUG, "release": R1_ID, "env": "preview"},
    }
    assert "confirm" not in call["arguments"]
    assert found.tools == ["deploy", "rollback"]
    assert mcp.closed


def test_the_mcp_child_is_closed_when_it_never_answers() -> None:
    mcp = FakeMcp({}, stderr="Code: AGENT_TOKEN_REQUIRED")
    found = rb.mcp_conversation(lambda argv: mcp, SLUG, R1_ID)
    assert found.initialized is False
    assert [m for m, _ in mcp.messages] == ["initialize"]
    assert mcp.closed
    assert "AGENT_TOKEN_REQUIRED" in found.stderr


def test_a_run_without_an_agent_login_is_incomplete_and_prints_the_prerequisite() -> None:
    mcp = FakeMcp({}, stderr="Code: AGENT_TOKEN_REQUIRED\n")
    outcome, _, _ = drive(World(), mcp)
    checks = by_n(outcome)
    assert checks[5]["result"] == "not read"
    assert outcome.passed is None
    assert any("ssc login --org <org id> --agent ga45" in line for line in outcome.lines)
    assert checks[7]["result"] == "PASS"


def test_a_failed_first_deploy_stops_the_rest() -> None:
    world = World(deploy1=Done(1, "", "boom"))
    outcome, _, mcp = drive(world)
    checks = by_n(outcome)
    assert outcome.passed is False
    assert checks[1]["result"] == "FAIL"
    assert [checks[n]["result"] for n in range(2, 10)] == ["not read"] * 8
    assert world.deploys == 1
    assert len(world.calls) == 1
    assert mcp.messages == []


def test_a_failed_second_deploy_stops_before_any_rollback() -> None:
    world = World(deploy2=sent(6, R2_ID, "failed"))
    outcome, _, _ = drive(world)
    assert by_n(outcome)[2]["result"] == "FAIL"
    assert all("rollback" not in c for c in world.calls)


def test_a_rollback_that_is_not_refused_fails_check_3() -> None:
    world = World(refuse_text=Done(0, "", "R5 runs again"))
    outcome, _, _ = drive(world)
    assert by_n(outcome)[3]["result"] == "FAIL"
    assert outcome.passed is False


def test_the_forward_rollback_runs_even_when_the_confirm_fails() -> None:
    world = World(confirm=Done(1, "", "Error: x"))
    outcome, _, _ = drive(world)
    checks = by_n(outcome)
    assert checks[7]["result"] == "FAIL"
    assert checks[6]["result"] == "not read"
    assert checks[8]["result"] == "not read"
    assert checks[9]["result"] == "PASS"


def test_audit_rows_skip_lines_that_are_not_rows() -> None:
    text = json.dumps(ROW) + "\nnot json\n[1]\n" + json.dumps({"x": 1}) + "\n\n"
    assert rb.audit_rows(text) == [ROW]


def release() -> rb.Release:
    return rb.Release(5, R1_ID, APP_ID, ENV_ID)


def test_started_for_keeps_only_this_release_and_environment() -> None:
    other_env = {**ROW, "after": {**AFTER, "environment_id": "env_" + "z" * 20}}
    other_rel = {**ROW, "after": {**AFTER, "release_id": R2_ID}}
    other_action = {**ROW, "action": "deploy.started"}
    rows = [ROW, other_env, other_rel, other_action, {"action": "rollback.started"}]
    assert rb.started_for(rows, release()) == [ROW]


def test_check_untouched() -> None:
    assert rb.check_untouched([ROW], OP).result is True
    assert rb.check_untouched([ROW, ROW], OP).result is False
    assert rb.check_untouched([], OP).result is False
    elsewhere = {**ROW, "target": {"kind": "deployment", "id": "dep_" + "z" * 20}}
    assert rb.check_untouched([elsewhere], OP).result is False


def test_check_audit_row() -> None:
    good = rb.check_audit_row([ROW], OP)
    assert good.result is True
    assert "user usr_x" in good.detail
    assert rb.check_audit_row([], OP).result is False
    for after in (
        {**AFTER, "confirmed": False},
        {**AFTER, "migrations_ahead": ["alembic:0001_ga45_first"]},
        {k: v for k, v in AFTER.items() if k != "migrations_ahead"},
    ):
        assert rb.check_audit_row([{**ROW, "after": after}], OP).result is False


def test_a_second_rollback_row_for_r1_fails_check_6() -> None:
    world = World()
    world.rows = [ROW, {**ROW, "seq": 6, "target": {"kind": "deployment", "id": "dep_" + "d" * 20}}]
    outcome, _, _ = drive(world)
    checks = by_n(outcome)
    assert checks[6]["result"] == "FAIL"
    assert checks[8]["result"] == "PASS"


def test_an_export_that_fails_leaves_the_audit_checks_unread() -> None:
    world = World(audit_fail=Done(1, "", "Error: forbidden"))
    outcome, _, _ = drive(world)
    checks = by_n(outcome)
    assert checks[6]["result"] == checks[8]["result"] == "not read"
    assert "ssc audit export failed" in checks[6]["detail"]
    assert outcome.passed is None


def test_the_export_is_asked_for_since_the_start_and_cleaned_up() -> None:
    world = World()
    drive(world)
    call = next(c for c in world.calls if "audit" in c)
    since = call[call.index("--since") + 1]
    assert since.endswith("Z") and "T" in since
    assert not Path(call[call.index("--out") + 1]).parent.exists()


def test_the_console_step_names_the_url_and_what_must_be_seen() -> None:
    outcome, _, _ = drive(World(), console="https://console.example.test/")
    text = "\n".join(outcome.lines)
    assert f"open https://console.example.test/apps/{APP_ID}" in text
    assert "pick R5" in text
    assert rb.AHEAD in text
    assert "do not tick" in text.lower()


def test_the_manual_check_does_not_set_the_verdict() -> None:
    manual = rb.Check(10, "x", None, "d", manual=True)
    passed = rb.Check(1, "x", True, "d")
    assert rb.verdict([passed, manual]) is True
    assert rb.verdict([passed, rb.Check(2, "x", None, "d"), manual]) is None
    assert rb.verdict([rb.Check(2, "x", False, "d"), rb.Check(3, "x", None, "d")]) is False
    assert manual.line().startswith("check 10 manual:")


def test_no_token_is_read_printed_or_saved(
    monkeypatch: pytest.MonkeyPatch, isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSC_TOKEN", TOKEN)
    outcome, said, _ = drive(World())
    emit(outcome)
    shown = capsys.readouterr().out + "\n".join(said)
    saved = "".join(p.read_text() for p in (isolated / "results").iterdir())
    assert TOKEN not in shown
    assert TOKEN not in saved
    assert saved
    assert any(p.name.startswith(f"rollback-{SLUG}-") for p in (isolated / "results").iterdir())
    assert (isolated / "results" / "ga-4.5.json").exists()


def test_a_fenced_argument_is_refused(fake_digest: str) -> None:
    args = argparse.Namespace(app=fake_digest, console_url=rb.CONSOLE_URL)
    with pytest.raises(FencedError):
        rb.run(args, run=World(), open_mcp=lambda argv: good_mcp(), say=lambda s: None)
    with pytest.raises(FencedError):
        rb.PipeMcp([sys.executable, "-c", f"print('{fake_digest}')"])


def test_the_command_is_registered_without_an_env_option() -> None:
    assert PROOFS["rollback"] is rb
    args = parser().parse_args(["rollback", "--app", SLUG])
    assert args.console_url == "https://console.delimitus.com"
    assert not hasattr(args, "env")
    with pytest.raises(SystemExit):
        parser().parse_args(["rollback", "--app", SLUG, "--env", "prod"])


def test_the_fixture_has_two_alembic_migrations_and_a_database() -> None:
    app = KIT / "apps" / "rollback"
    manifest = tomllib.loads((app / "ssc.toml").read_text())
    assert manifest["state"]["postgres"] is True
    versions = sorted(p.name for p in (app / "alembic" / "versions").glob("*.py"))
    assert versions == [f"{rb.FIRST}.py", f"{rb.AHEAD}.py"]
    assert (app / "alembic" / "env.py").exists()
    second = (app / "alembic" / "versions" / f"{rb.AHEAD}.py").read_text()
    assert f'revision = "{rb.AHEAD}"' in second
    assert f'down_revision = "{rb.FIRST}"' in second


ECHO = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    m = json.loads(line)\n"
    "    if 'id' in m:\n"
    "        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': {'echo': m['method']}}),"
    " flush=True)\n"
)


def test_pipe_mcp_speaks_newline_delimited_json() -> None:
    conn = rb.PipeMcp([sys.executable, "-I", "-c", ECHO])
    try:
        conn.notify("notifications/initialized")
        first = conn.request("initialize", {})
        second = conn.request("tools/list", {})
    finally:
        err = conn.close()
    assert first == {"jsonrpc": "2.0", "id": 1, "result": {"echo": "initialize"}}
    assert second is not None and second["id"] == 2
    assert err == ""
    assert conn.proc.poll() is not None


def test_pipe_mcp_returns_none_and_stderr_when_the_child_exits() -> None:
    code = "import sys; sys.stderr.write('Code: AGENT_TOKEN_REQUIRED\\n'); sys.exit(3)"
    conn = rb.PipeMcp([sys.executable, "-I", "-c", code])
    try:
        assert conn.request("initialize", {}) is None
    finally:
        err = conn.close()
    assert "AGENT_TOKEN_REQUIRED" in err


def test_pipe_mcp_stops_a_hung_child_at_the_limit() -> None:
    hang = "import time; time.sleep(60)"
    conn = rb.PipeMcp([sys.executable, "-I", "-c", hang], limit=0.5)
    try:
        assert conn.request("initialize", {}) is None
    finally:
        conn.close()
    assert conn.proc.poll() is not None
