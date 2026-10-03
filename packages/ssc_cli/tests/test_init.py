"""``ssc init``: the agent pack is idempotent, keeps the person's text, and names real commands."""

import ast
import re
from pathlib import Path

import pytest
from typer.core import TyperGroup
from typer.main import get_command

from ssc_cli.agentpack import guide
from ssc_cli.agentpack.content import BEGIN, END, STARTER_MANIFEST
from ssc_cli.doctor import run_doctor
from ssc_cli.doctor.finding import FIX
from ssc_cli.errors import ExitCode
from ssc_cli.main import app
from ssc_cli.shapes import InitResult
from ssc_contracts import app_env
from ssc_contracts.manifest import load_manifest

SRC = Path(__file__).resolve().parents[1] / "src" / "ssc_cli"
SKILL = ".claude/skills/ssc/SKILL.md"
USER_TEXT = "# Team notes\n\nDeploy on Fridays only after lunch.\n"


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
        "ssc.toml": "created",
        ".sscignore": "created",
    }
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
    blocks = re.findall(r"(?s)```toml\n(.*?)```", guide([]))
    assert blocks
    for block in blocks:
        assert load_manifest(block).state.postgres
    for text in (guide([]), STARTER_MANIFEST, *FIX.values()):
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
