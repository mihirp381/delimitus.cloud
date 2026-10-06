"""The nightly workflows keep their guards, their one schedule and their secrets out of files.

The uv environment has no YAML reader, so these read the workflow files as text.
"""

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
NIGHT_FILES = ("nightly.yml", "isolation-nightly.yml", "kill-drill.yml")
RETIRED = (
    "SSC_ISO_CELL1_KEYRING",
    "SSC_ISO_CELL1_ORG",
    "SSC_ISO_CELL1_BASE",
    "SSC_ISO_CELL2_BASE",
    "SSC_DRILL_KEYRING",
    "SSC_DRILL_USER",
    "SSC_DRILL_TOKEN",
    "secrets.SSC_ISO_AUTH_STATE",
)
UPLOADS = {
    "${{ runner.temp }}/evidence/probes.json",
    "${{ runner.temp }}/evidence/browser-${{ inputs.position }}.json",
    "${{ runner.temp }}/evidence/drill-${{ inputs.position }}.json",
    "${{ runner.temp }}/page.md",
}
SHA = re.compile(r"@[0-9a-f]{40}( |$)")


def lines_of(name: str) -> list[str]:
    return (WORKFLOWS / name).read_text(encoding="utf-8").splitlines()


def code(name: str) -> list[str]:
    """The file's lines without full-line comments."""
    return [line for line in lines_of(name) if not line.lstrip().startswith("#")]


def jobs(name: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    inside = False
    current = ""
    for line in code(name):
        if line == "jobs:":
            inside = True
        elif inside and re.match(r"^  [\w-]+:\s*$", line):
            current = line.strip().rstrip(":")
            found[current] = []
        elif inside and current:
            found[current].append(line)
    return found


def run_scripts(name: str) -> list[str]:
    """The shell text of every `run:` step."""
    out: list[str] = []
    lines = code(name)
    for i, line in enumerate(lines):
        stripped = line.strip().removeprefix("- ")
        if not stripped.startswith("run:"):
            continue
        body = stripped.removeprefix("run:").strip()
        if body not in {"|", ">"}:
            out.append(body)
            continue
        indent = len(line) - len(line.lstrip())
        for later in lines[i + 1 :]:
            if later.strip() and len(later) - len(later.lstrip()) <= indent:
                break
            out.append(later)
    return out


def test_nightly_is_the_only_schedule() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        scheduled = any(
            line.strip().startswith(("schedule:", "- cron:")) for line in code(path.name)
        )
        assert scheduled == (path.name == "nightly.yml"), path.name


@pytest.mark.parametrize("name", NIGHT_FILES)
def test_every_job_runs_on_main_only_and_inert_without_the_cells(name: str) -> None:
    found = jobs(name)
    assert found
    for job, body in found.items():
        guard = next((line for line in body if re.match(r"^    if:", line)), "")
        assert "github.ref == 'refs/heads/main'" in guard, f"{name}: {job}"
    if name != "nightly.yml":
        assert all("vars.SSC_NIGHT_CELLS != ''" in "".join(b) for b in found.values())


def test_the_two_called_workflows_can_also_be_run_by_hand() -> None:
    for name in ("isolation-nightly.yml", "kill-drill.yml"):
        text = "\n".join(code(name))
        assert "  workflow_call:" in text
        assert "  workflow_dispatch:" in text
    calls = "\n".join(code("nightly.yml"))
    assert "uses: ./.github/workflows/isolation-nightly.yml" in calls
    assert "uses: ./.github/workflows/kill-drill.yml" in calls


@pytest.mark.parametrize("name", NIGHT_FILES)
def test_the_retired_settings_are_gone(name: str) -> None:
    text = "\n".join(code(name))
    for retired in RETIRED:
        assert retired not in text, f"{name}: {retired}"


def test_artifacts_hold_evidence_and_the_page_only() -> None:
    for name in NIGHT_FILES:
        lines = code(name)
        for i, line in enumerate(lines):
            if "actions/upload-artifact@" not in line:
                continue
            paths = [
                x.strip().removeprefix("path:").strip() for x in lines[i : i + 8] if "path:" in x
            ]
            assert paths
            assert set(paths) <= UPLOADS, f"{name}: {paths}"


def test_the_password_is_a_step_level_variable_only() -> None:
    for name in NIGHT_FILES:
        for line in code(name):
            if "SSC_NIGHT_PASSWORD" in line:
                assert re.match(r"^          SSC_NIGHT_PASSWORD: \$\{\{ ", line), f"{name}: {line}"
    for name in ("isolation-nightly.yml", "kill-drill.yml"):
        for script in run_scripts(name):
            assert "PASSWORD" not in script


def test_the_sign_in_files_are_deleted_even_when_the_job_fails() -> None:
    expected = {
        "isolation-nightly.yml": ("auth-state.json",),
        "kill-drill.yml": ("drill-auth-state.json", "drill-credentials.json"),
    }
    for name, files in expected.items():
        lines = code(name)
        last = next(i for i, line in enumerate(lines) if "Clear the sign-in files" in line)
        step = "\n".join(lines[last:])
        assert "if: always()" in step
        for file in files:
            assert file in step
        assert not any(x.startswith("      - ") for x in lines[last + 1 :]), "it is the last step"


@pytest.mark.parametrize("name", NIGHT_FILES)
def test_actions_are_pinned_and_run_steps_have_no_expressions(name: str) -> None:
    for line in code(name):
        if "uses:" in line and "uses: ./" not in line:
            assert SHA.search(line), f"{name}: {line.strip()}"
    for script in run_scripts(name):
        assert "${{" not in script, f"{name}: {script.strip()}"
