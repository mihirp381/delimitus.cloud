"""The ssc-deploy GitHub Action: run.py against a fake ssc, and once for real on the dev stack.

The fake ssc records its argv and environment, then prints and exits as the test says. The live
test is the CI ``action`` job without the runner: a preview-scoped token deploys a folder through
run.py and the real ``ssc``, and the outputs name the healthy preview.
"""

import importlib.util
import io
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ssc_cli.shapes import AppResult
from ssc_contracts.ids import prefix_of

ROOT = Path(__file__).resolve().parents[3]
ACTION = ROOT / ".github" / "actions" / "ssc-deploy"
SHA = "0123456789abcdef0123456789abcdef01234567"
URL = "https://demo--preview.abcdefghijkl.delimitusapps.com"
RELEASE = "rel_aaaaaaaaaaaaaaaaaaaa"
OPERATION = "dep_bbbbbbbbbbbbbbbbbbbb"
FAKE = """\
import json, os, signal, sys
with open(os.environ["FAKE_RECORD"], "w") as f:
    json.dump({"argv": sys.argv[1:], "token": os.environ.get("SSC_TOKEN"),
               "api": os.environ.get("SSC_API_URL")}, f)
sys.stdout.write(os.environ.get("FAKE_STDOUT", ""))
sys.stderr.write(os.environ.get("FAKE_STDERR", ""))
if os.environ.get("FAKE_EXIT") == "signal":
    os.kill(os.getpid(), signal.SIGKILL)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


@pytest.fixture(scope="module")
def run() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ssc_deploy_run", ACTION / "run.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(**changes: Any) -> dict[str, Any]:
    return {
        "app_id": "app_cccccccccccccccccccc",
        "slug": "demo",
        "environment": "preview",
        "release_id": RELEASE,
        "release_number": 3,
        "operation_id": OPERATION,
        "state": "healthy",
        "url": URL,
        "warnings": [],
        "capability_changes": [],
    } | changes


class Step:
    """One run of run.py in a fake runner: its env, log, outputs and summary files."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        fake = tmp_path / "ssc"
        fake.write_text(f"#!{sys.executable}\n{FAKE}")
        fake.chmod(0o755)
        self.record = tmp_path / "record.json"
        self.outputs = tmp_path / "outputs"
        self.summary = tmp_path / "summary.md"
        self.env = {
            "PATH": os.environ["PATH"],
            "SSC_BIN": str(fake),
            "SSC_TOKEN": "tok-123",
            "SSC_API_URL": "https://api.test",
            "SSC_APP": "demo",
            "SSC_PATH": "web",
            "SSC_COMMIT": SHA,
            "GITHUB_OUTPUT": str(self.outputs),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "FAKE_RECORD": str(self.record),
        }
        self.log = ""

    def __call__(self, run: ModuleType, stdout: str = "", **env: str) -> int:
        out = io.StringIO()
        code = run.main({**self.env, "FAKE_STDOUT": stdout, **env}, out)
        self.log = out.getvalue()
        return code

    def called(self) -> dict[str, Any]:
        return json.loads(self.record.read_text())


def _outputs(text: str) -> dict[str, str]:
    found = re.findall(r"([a-z-]+)<<(ghadelimiter_[0-9a-f-]{36})\n(.*?)\n\2\n", text, re.S)
    return {name: value for name, _, value in found}


@pytest.fixture
def step(tmp_path: Path) -> Step:
    return Step(tmp_path)


def test_a_deploy_fills_the_outputs_the_log_and_the_summary(run, step):
    result = _result(
        warnings=[{"path": "app.py", "line": 4, "rule": "generic-api-key", "masked": "sk_…9f"}],
        capability_changes=[{"consequence": "The app can now reach the orders database."}],
    )
    assert step(run, json.dumps(result)) == 0
    assert step.called() == {
        "argv": ["deploy", "--app=demo", "--json", "--wait", f"--commit={SHA}", "--", "web"],
        "token": "tok-123",
        "api": "https://api.test",
    }
    assert _outputs(step.outputs.read_text()) == {
        "preview-url": URL,
        "release-id": RELEASE,
        "operation-id": OPERATION,
    }
    lines = step.log.splitlines()
    assert lines[0] == "::add-mask::tok-123"
    assert f"Preview: {URL}" in lines
    assert "Change: The app can now reach the orders database." in lines
    assert (
        "::warning title=ssc secret scan::app.py:4 looks like a secret (generic-api-key: sk_…9f),"
        " not blocking." in lines
    )
    summary = step.summary.read_text()
    assert summary.startswith("### ssc deploy\n")
    assert f"Preview: {URL}" in summary
    assert f"R3 ({RELEASE}) of demo is healthy in preview." in summary


def test_without_a_commit_or_path_it_deploys_the_workspace_with_no_commit(run, step):
    assert step(run, json.dumps(_result()), SSC_COMMIT="", SSC_PATH="") == 0
    assert step.called()["argv"] == ["deploy", "--app=demo", "--json", "--wait", "--", "."]


def test_an_app_with_no_url_leaves_preview_url_empty_and_says_so(run, step):
    assert step(run, json.dumps(_result(url=None))) == 0
    assert _outputs(step.outputs.read_text())["preview-url"] == ""
    assert "::warning::The API gave no preview URL for this app." in step.log.splitlines()


def test_a_refusal_is_one_error_annotation_with_ssc_s_exit_code(run, step):
    error = {
        "code": "SECRET_FOUND",
        "title": "The bundle holds a secret.",
        "detail": "app.py:4 matches aws-access-key.",
        "status": None,
        "request_id": "req-9",
    }
    assert step(run, json.dumps({"error": error}), FAKE_EXIT="4") == 4
    errors = [line for line in step.log.splitlines() if line.startswith("::error")]
    assert errors == [
        "::error title=ssc deploy failed::SECRET_FOUND: The bundle holds a secret."
        " app.py:4 matches aws-access-key. (request req-9)"
    ]
    assert not step.outputs.exists()
    assert not step.summary.exists()


def test_a_failure_without_json_names_the_exit_code_and_passes_stderr_on(run, step):
    assert step(run, FAKE_STDERR="Error: Invalid value for '--commit'\n", FAKE_EXIT="2") == 2
    assert "ssc: Error: Invalid value for '--commit'" in step.log.splitlines()
    assert "::error title=ssc deploy failed::ssc deploy exited 2; its messages are above." in (
        step.log.splitlines()
    )


def test_ssc_killed_by_a_signal_fails_the_step(run, step):
    assert step(run, FAKE_EXIT="signal") == 1


def test_nothing_from_ssc_or_the_api_starts_a_workflow_command(run, step):
    sneaky = "x\n::set-output name=preview-url::https://evil.test\n::add-mask::"
    error = {"code": "X", "title": "50% done\r\n::warning::no", "detail": sneaky}
    assert step(run, json.dumps({"error": error}), FAKE_STDERR=sneaky, FAKE_EXIT="1") == 1
    commands = [line for line in step.log.splitlines() if line.startswith("::")]
    assert [c.split("::")[1] for c in commands] == ["add-mask", "error title=ssc deploy failed"]
    assert "50%25 done ::warning::no" in commands[1]
    assert all(line.startswith("ssc: ") for line in step.log.splitlines()[2:-1])

    evil = _result(url=f"{URL}\n::error::boom", slug="demo\n::error::boom")
    assert step(run, json.dumps(evil)) == 0
    assert not any(line.startswith("::error") for line in step.log.splitlines())
    assert _outputs(step.outputs.read_text())["preview-url"] == f"{URL} ::error::boom"


@pytest.mark.parametrize("key", ["SSC_TOKEN", "SSC_APP"])
def test_an_empty_input_is_refused_before_ssc_runs(run, step, key):
    assert step(run, **{key: " "}) == 2
    name = {"SSC_TOKEN": "token", "SSC_APP": "app"}[key]
    assert step.log.startswith(f"::error title=ssc deploy::The {name} input is empty.")
    assert step.log.count("\n") == 1
    assert not step.record.exists()


def test_a_missing_ssc_fails_the_step(run, step):
    assert step(run, SSC_BIN=str(step.dir / "absent")) == 1
    assert "::error title=ssc deploy::Could not run" in step.log


def test_exit_0_without_a_deploy_result_fails_the_step(run, step):
    assert step(run, "not json") == 1
    assert step(run, json.dumps({"release_id": RELEASE})) == 1
    assert not step.outputs.exists()


def test_every_input_reaches_the_steps_through_env(run):
    text = (ACTION / "action.yml").read_text()
    inputs = re.search(r"^inputs:\n(.*?)^\S", text, re.S | re.M)
    assert inputs
    declared = set(re.findall(r"^  ([a-z-]+):$", inputs.group(1), re.M))
    used = dict(re.findall(r"^\s+([A-Z_]+): \$\{\{ inputs\.([a-z-]+) \}\}$", text, re.M))
    assert set(used.values()) == declared
    assert {k for k in used if k.startswith("SSC_")} == {
        "SSC_TOKEN",
        "SSC_API_URL",
        "SSC_APP",
        "SSC_PATH",
        "SSC_COMMIT",
    }
    runs = re.findall(r"run: \|\n((?:\s{8}.*\n)+)", text)
    assert runs
    assert all("${{" not in r for r in runs)


def test_an_app_or_commit_that_looks_like_an_option_stays_a_value(run, step):
    assert step(run, json.dumps(_result()), SSC_APP="--help", SSC_COMMIT="--json") == 0
    argv = step.called()["argv"]
    assert argv == ["deploy", "--app=--help", "--json", "--wait", "--commit=--json", "--", "web"]


def test_job_level_env_in_ci_uses_only_contexts_github_allows_there():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    blocks = re.findall(r"^    env:\n((?:      .*\n)+)", ci, re.M)
    assert blocks
    used = set(re.findall(r"\$\{\{\s*([a-z_]+)\.", "".join(blocks)))
    assert used <= {"github", "needs", "strategy", "matrix", "vars", "secrets", "inputs"}


def _runner_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(("SSC_", "GITHUB_"))}


def test_live_a_preview_token_deploys_through_the_action(run, cli, live, tmp_path, monkeypatch):
    slug = f"a{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("SSC_TOKEN", live.token())
    created = cli("--api", live.url, "apps", "create", slug, "--json")
    assert created.code == 0, created.stderr
    folder = tmp_path / "web"
    folder.mkdir()
    (folder / "ssc.toml").write_text('schema = "ssc/v1"\n')
    (folder / "main.py").write_text("print('hello')\n")
    ssc = shutil.which("ssc", path=str(Path(sys.executable).parent))
    assert ssc
    outputs = tmp_path / "outputs"
    env = _runner_env() | {
        "SSC_BIN": ssc,
        "SSC_TOKEN": live.token(scope="preview"),
        "SSC_API_URL": live.url,
        "SSC_APP": slug,
        "SSC_PATH": str(folder),
        "SSC_COMMIT": SHA,
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }
    log = io.StringIO()
    assert run.main(env, log) == 0, log.getvalue()
    values = _outputs(outputs.read_text())
    assert re.match(rf"^https://{slug}--preview\.", values["preview-url"])
    assert (prefix_of(values["release-id"]), prefix_of(values["operation-id"])) == ("rel", "dep")

    status = AppResult.model_validate(cli("--api", live.url, "status", slug, "--json").json())
    envs = {e.name: e for e in status.environments}
    preview = envs["preview"].deployment
    assert preview is not None
    assert (preview.state, preview.release_id) == ("healthy", values["release-id"])
    assert envs["preview"].url == values["preview-url"]
    assert envs["prod"].deployment is None
