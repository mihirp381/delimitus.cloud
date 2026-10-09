"""``ssc init``: the agent pack is idempotent, keeps the person's text, and names real commands."""

import ast
import json
import re
from pathlib import Path

import httpx2
import pytest
from typer.core import TyperGroup
from typer.main import get_command

from ssc_cli.agentpack import guide
from ssc_cli.agentpack.content import BEGIN, END, STARTER_MANIFEST
from ssc_cli.config import config_path
from ssc_cli.doctor import run_doctor
from ssc_cli.doctor.finding import FIX
from ssc_cli.errors import ExitCode
from ssc_cli.main import app
from ssc_cli.session import Session
from ssc_cli.shapes import ErrorResult, InitResult
from ssc_contracts import app_env
from ssc_contracts.manifest import load_manifest

SRC = Path(__file__).resolve().parents[1] / "src" / "ssc_cli"
SKILL = ".claude/skills/ssc/SKILL.md"
USER_TEXT = "# Team notes\n\nDeploy on Fridays only after lunch.\n"
MCP_URL = "https://api.delimitus.com/mcp"
MCP_FILES = {
    ".mcp.json": {"type": "http", "url": MCP_URL},
    ".cursor/mcp.json": {"url": MCP_URL},
}


def init(cli, root: Path, *args: str) -> InitResult:
    r = cli("init", str(root), "--json", *args)
    assert r.code == 0, r.stdout + r.stderr
    return InitResult.model_validate(r.json())


def actions(result: InitResult) -> dict[str, str]:
    return {f.path: f.action for f in result.files}


def test_init_writes_the_pack(cli, tmp_path):
    first = init(cli, tmp_path)
    assert actions(first) == {
        "AGENTS.md": "created",
        SKILL: "created",
        ".mcp.json": "created",
        ".cursor/mcp.json": "created",
        "ssc.toml": "created",
        ".sscignore": "created",
    }
    assert [f.path for f in first.files][2:4] == [".mcp.json", ".cursor/mcp.json"]
    assert (tmp_path / ".mcp.json").read_text() == (
        '{\n  "mcpServers": {\n    "ssc": {\n      "type": "http",\n'
        '      "url": "https://api.delimitus.com/mcp"\n    }\n  }\n}\n'
    )
    assert (tmp_path / ".cursor/mcp.json").read_text() == (
        '{\n  "mcpServers": {\n    "ssc": {\n'
        '      "url": "https://api.delimitus.com/mcp"\n    }\n  }\n}\n'
    )
    agents = (tmp_path / "AGENTS.md").read_text()
    assert agents.startswith(BEGIN)
    assert agents.endswith(f"{END}\n")
    assert "{commands}" not in agents
    assert '`{"error": {...}}`' in agents
    assert "- `ssc doctor [PATH]`:" in agents
    skill = (tmp_path / SKILL).read_text()
    assert skill.startswith("---\nname: ssc\ndescription: ")
    body = agents.removeprefix(f"{BEGIN}\n").removesuffix(f"{END}\n")
    assert skill.endswith(f"---\n\n{body}")
    assert (tmp_path / "ssc.toml").read_text() == STARTER_MANIFEST
    assert not (tmp_path / "CLAUDE.md").exists()


def test_init_idempotent(cli, tmp_path):
    (tmp_path / "AGENTS.md").write_text(USER_TEXT)
    (tmp_path / "CLAUDE.md").write_text("Be brief.")
    (tmp_path / "ssc.toml").write_text('schema = "ssc/v1"\n# mine\n')
    first = init(cli, tmp_path)
    assert actions(first)["AGENTS.md"] == "updated"
    assert actions(first)["CLAUDE.md"] == "updated"
    assert actions(first)["ssc.toml"] == "unchanged"
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    second = init(cli, tmp_path)
    assert set(actions(second).values()) == {"unchanged"}
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == snapshot

    agents = (tmp_path / "AGENTS.md").read_text()
    assert agents.startswith(USER_TEXT + "\n" + BEGIN)
    assert agents.count(BEGIN) == 1
    assert (tmp_path / "CLAUDE.md").read_text() == f"Be brief.\n\n{BEGIN}\n@AGENTS.md\n{END}\n"
    assert (tmp_path / "ssc.toml").read_text() == 'schema = "ssc/v1"\n# mine\n'


def test_changed_block_is_kept_unless_forced(cli, tmp_path):
    init(cli, tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        USER_TEXT
        + "\n"
        + agents.read_text().replace("Run `ssc doctor`", "Run it")
        + "\nMore notes.\n"
    )
    (tmp_path / SKILL).write_text("edited")
    before = agents.read_text()

    kept = init(cli, tmp_path)
    notes = {f.path: f.note for f in kept.files if f.action == "skipped"}
    assert set(notes) == {"AGENTS.md", SKILL}
    assert "`ssc init --force`" in (notes["AGENTS.md"] or "")
    assert agents.read_text() == before

    forced = init(cli, tmp_path, "--force")
    assert actions(forced)["AGENTS.md"] == "updated"
    assert actions(forced)[SKILL] == "updated"
    text = agents.read_text()
    assert text.startswith(USER_TEXT)
    assert text.endswith(f"{END}\n\nMore notes.\n")
    assert "Run `ssc doctor`" in text
    assert set(actions(init(cli, tmp_path)).values()) == {"unchanged"}


@pytest.mark.parametrize(
    "broken",
    [
        f"{BEGIN}\nno end marker\n",
        f"{BEGIN}\na\n{END}\n{BEGIN}\nb\n{END}\n",
        f"stray end\n{END}\n",
        f"{BEGIN}\na\n{END}\n<!-- ssc:begin v2 -->\n",
    ],
)
def test_broken_markers_are_left_alone(cli, tmp_path, broken):
    (tmp_path / "AGENTS.md").write_text(broken)
    for extra in ((), ("--force",)):
        result = init(cli, tmp_path, *extra)
        (f,) = [f for f in result.files if f.path == "AGENTS.md"]
        assert f.action == "skipped"
        assert "broken or repeated" in (f.note or "")
        assert (tmp_path / "AGENTS.md").read_text() == broken


def test_unreadable_file_fails_the_command(cli, tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"\xff\xfe not utf-8")
    r = cli("init", str(tmp_path), "--json")
    assert r.code == ExitCode.FAILED
    files = {f.path: f for f in InitResult.model_validate(r.json()).files}
    assert files["AGENTS.md"].action == "skipped"
    assert "cannot update" in (files["AGENTS.md"].note or "")
    assert files[SKILL].action == "created"


def test_human_output(cli, tmp_path):
    r = cli("init", str(tmp_path))
    assert r.code == 0
    assert "created    AGENTS.md" in r.stdout


# ── MCP settings: shared files, only the ssc server is ours ─────────────────


def _mcp_action(result: InitResult, rel: str) -> tuple[str, str | None]:
    (f,) = [f for f in result.files if f.path == rel]
    return f.action, f.note


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=4))


@pytest.mark.parametrize("rel", MCP_FILES)
def test_mcp_settings_keep_other_servers_and_keys(cli, tmp_path, rel):
    other = {"command": "other-tool", "args": ["serve"]}
    _write_json(tmp_path / rel, {"$comment": "mine", "mcpServers": {"other": other}, "z": [1]})

    first = init(cli, tmp_path)
    assert _mcp_action(first, rel) == ("updated", None)
    text = (tmp_path / rel).read_text()
    assert json.loads(text) == {
        "$comment": "mine",
        "mcpServers": {"other": other, "ssc": MCP_FILES[rel]},
        "z": [1],
    }
    assert list(json.loads(text)) == ["$comment", "mcpServers", "z"]
    assert list(json.loads(text)["mcpServers"]) == ["other", "ssc"]
    assert text.startswith('{\n  "$comment"') and text.endswith("}\n")

    second = init(cli, tmp_path)
    assert _mcp_action(second, rel) == ("unchanged", None)
    assert (tmp_path / rel).read_text() == text


@pytest.mark.parametrize("rel", MCP_FILES)
def test_mcp_settings_without_servers_get_them(cli, tmp_path, rel):
    _write_json(tmp_path / rel, {"other": 1})
    assert _mcp_action(init(cli, tmp_path), rel) == ("updated", None)
    data = json.loads((tmp_path / rel).read_text())
    assert data == {"other": 1, "mcpServers": {"ssc": MCP_FILES[rel]}}
    assert list(data) == ["other", "mcpServers"]


@pytest.mark.parametrize("rel", MCP_FILES)
def test_changed_ssc_server_is_kept_unless_forced(cli, tmp_path, rel):
    mine = {"url": "https://elsewhere.example.com/mcp"}
    _write_json(tmp_path / rel, {"mcpServers": {"ssc": mine, "other": {"url": "https://o.test"}}})
    before = (tmp_path / rel).read_text()

    action, note = _mcp_action(init(cli, tmp_path), rel)
    assert action == "skipped"
    assert note == "the ssc server differs; run `ssc init --force` to replace it"
    assert (tmp_path / rel).read_text() == before

    assert _mcp_action(init(cli, tmp_path, "--force"), rel) == ("updated", None)
    data = json.loads((tmp_path / rel).read_text())
    assert data == {"mcpServers": {"ssc": MCP_FILES[rel], "other": {"url": "https://o.test"}}}
    assert list(data["mcpServers"]) == ["ssc", "other"]
    assert _mcp_action(init(cli, tmp_path), rel) == ("unchanged", None)


@pytest.mark.parametrize("rel", MCP_FILES)
@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("{not json", "not valid JSON"),
        ("", "not valid JSON"),
        ("  \n", "not valid JSON"),
        ("[]", "not a JSON object"),
        ('"ssc"', "not a JSON object"),
        ('{"mcpServers": []}', "`mcpServers` is not a JSON object"),
        ('{"mcpServers": "ssc"}', "`mcpServers` is not a JSON object"),
    ],
)
def test_mcp_settings_ssc_cannot_read_are_left_alone(cli, tmp_path, rel, text, problem):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    for extra in ((), ("--force",)):
        action, note = _mcp_action(init(cli, tmp_path, *extra), rel)
        assert action == "skipped"
        assert problem in (note or "")
        assert "fix it by hand" in (note or "")
        assert path.read_text() == text


def test_unreadable_mcp_settings_fail_the_command(cli, tmp_path):
    (tmp_path / ".mcp.json").write_bytes(b"\xff\xfe not utf-8")
    r = cli("init", str(tmp_path), "--json")
    assert r.code == ExitCode.FAILED
    files = {f.path: f for f in InitResult.model_validate(r.json()).files}
    assert files[".mcp.json"].action == "skipped"
    assert "cannot update" in (files[".mcp.json"].note or "")
    assert files[".cursor/mcp.json"].action == "created"
    assert (tmp_path / ".mcp.json").read_bytes() == b"\xff\xfe not utf-8"


def _mcp_urls(root: Path) -> set[str]:
    claude = json.loads((root / ".mcp.json").read_text())["mcpServers"]["ssc"]
    cursor = json.loads((root / ".cursor/mcp.json").read_text())["mcpServers"]["ssc"]
    return {claude["url"], cursor["url"]}


def test_mcp_url_follows_api_option(cli, tmp_path):
    r = cli("--api", "https://ssc.example.com/", "init", str(tmp_path), "--json")
    assert r.code == 0, r.stdout + r.stderr
    assert _mcp_urls(tmp_path) == {"https://ssc.example.com/mcp"}


def test_mcp_url_follows_env(cli, tmp_path, monkeypatch):
    monkeypatch.setenv("SSC_API_URL", "https://env.example.com")
    init(cli, tmp_path)
    assert _mcp_urls(tmp_path) == {"https://env.example.com/mcp"}


def test_mcp_url_follows_config_file(cli, tmp_path):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('api_url = "https://file.example.com"\n')
    init(cli, tmp_path)
    assert _mcp_urls(tmp_path) == {"https://file.example.com/mcp"}


def test_bad_api_url_writes_nothing(cli, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    r = cli("--api", "http://example.com", "init", str(root), "--json")
    assert r.code == ExitCode.USAGE
    assert ErrorResult.model_validate(r.json()).error.code == "BAD_API_URL"
    assert list(root.iterdir()) == []


class _NoNetwork(httpx2.BaseTransport):
    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"ssc init sent {request.method} {request.url}")


def test_init_needs_no_login_and_sends_nothing(cli, tmp_path):
    r = cli("init", str(tmp_path), "--json", session=Session(transport=_NoNetwork()))
    assert r.code == 0, r.stdout + r.stderr
    assert _mcp_urls(tmp_path) == {MCP_URL}


# ── only real commands ──────────────────────────────────────────────────────

_MENTION = re.compile(r"`ssc ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?")


def _registered() -> dict[str, set[str] | None]:
    root = get_command(app)
    assert isinstance(root, TyperGroup)
    return {
        name: set(cmd.commands) if isinstance(cmd, TyperGroup) else None
        for name, cmd in root.commands.items()
    }


def _user_facing_strings() -> list[str]:
    """Every string literal in the CLI source except docstrings."""
    found: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
        found += [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
        ]
    return found


def test_pack_mentions_only_real_commands(cli, tmp_path):
    init(cli, tmp_path)
    texts = [
        (tmp_path / "AGENTS.md").read_text(),
        (tmp_path / SKILL).read_text(),
        (tmp_path / "ssc.toml").read_text(),
        *FIX.values(),
        *_user_facing_strings(),
    ]
    registered = _registered()
    mentions = {m.groups() for text in texts for m in _MENTION.finditer(text)}
    assert ("doctor", None) in mentions
    for command, sub in mentions:
        assert command in registered, f"`ssc {command}` is not a command"
        subs = registered[command]
        if subs is not None and sub is not None:
            assert sub in subs, f"`ssc {command} {sub}` is not a command"


def test_pack_lists_every_command(cli, tmp_path):
    init(cli, tmp_path)
    agents = (tmp_path / "AGENTS.md").read_text()
    listed = set(re.findall(r"(?m)^- `ssc ([a-z]+(?: [a-z]+)?)", agents))
    expected = set()
    for name, subs in _registered().items():
        if subs is None or name == "apps":
            expected.add(name)
        expected |= {f"{name} {s}" for s in subs or ()}
    assert listed == expected


_EXAMPLE = re.compile(r"(?m)^# (\[[a-z_.]+\]|[a-z_]+ = .+)$")


def test_starter_manifest_validates(cli, tmp_path):
    load_manifest(STARTER_MANIFEST)
    examples = load_manifest(_EXAMPLE.sub(r"\1", STARTER_MANIFEST))
    assert examples.state.postgres
    assert examples.runtime.start
    blocks = re.findall(r"(?s)```toml\n(.*?)```", guide([], MCP_URL))
    assert blocks
    for block in blocks:
        assert load_manifest(block).state.postgres
    for text in (guide([], MCP_URL), STARTER_MANIFEST, *FIX.values()):
        assert 'state = "' not in text
    init(cli, tmp_path)
    assert not [f for f in run_doctor(tmp_path) if f.code.startswith("MANIFEST")]


def test_pack_names_the_platform_env(cli, tmp_path):
    init(cli, tmp_path)
    agents = (tmp_path / "AGENTS.md").read_text()
    for name in app_env.PLATFORM_ENV_NAMES:
        assert f"`{name}`" in agents, name
    assert f"`{app_env.HOME}` is `{app_env.HOME_VALUE}`" in agents
    assert "--port $PORT" in agents
    assert "not fixed yet" not in agents


_SPAN = re.compile(r"`(ssc [^`]+)`")


def test_pack_uses_only_real_options(cli, tmp_path):
    init(cli, tmp_path)
    root = get_command(app)
    assert isinstance(root, TyperGroup)
    checked = set()
    for span in _SPAN.findall((tmp_path / "AGENTS.md").read_text()):
        words = span.split()
        cmd = root.commands.get(words[1])
        if cmd is None:
            continue
        if isinstance(cmd, TyperGroup) and len(words) > 2 and words[2] in cmd.commands:
            cmd = cmd.commands[words[2]]
        known = {o for p in cmd.params for o in p.opts}
        for word in words:
            if word.startswith("--"):
                assert word in known, f"`{span}`: {word} is not an option"
                checked.add((cmd.name, word))
    assert {("logs", "--env"), ("set", "--env"), ("deploy", "--app")} <= checked


def test_pack_gives_the_session_and_cold_start_facts(cli, tmp_path):
    init(cli, tmp_path)
    agents = (tmp_path / "AGENTS.md").read_text()
    for fact in (
        '"waking up" page',
        "one instance",
        "closed after 60 minutes",
        "Streamlit loses its session state",
        "SESSION_FRAMEWORK",
        "end_before_deadline(events, request.headers)",
        "closeBeforeDeadline(socket, req.headers)",
        "sscSocket(url, { onopen, onmessage })",
        "STATE_SQLITE_EPHEMERAL",
        "set every connection pool to 1",
        "creates the company's database",
        "`ssc logs <app>",
        "`ssc secret set <app> NAME --env <env>`",
    ):
        assert fact in " ".join(agents.split()), fact
