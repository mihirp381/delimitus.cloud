"""The probe's runner and log reader with a fake shell, and its program under Pulumi mocks. No
Pulumi, gcloud or network command runs.

cd infra && uv run pytest ../spikes/sinkhole -p no:cacheprovider
"""

import asyncio
import json
import sys
import zlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pulumi
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
INFRA = HERE.parent.parent.parent / "infra"
sys.path[:0] = [str(INFRA), str(INFRA / "tests")]
import logsummary  # noqa: E402
import mockcloud  # noqa: E402
import program  # noqa: E402
import run as probe  # noqa: E402

from ssc_infra import naming  # noqa: E402

TOKEN = "ya29.fake-token"


class FakeShell:
    """Records every command. ``fail`` maps a command word (``up``, ``destroy``, ``init``) to an
    exit code, or to an exception to raise."""

    def __init__(self, fail: Mapping[str, int | BaseException] | None = None) -> None:
        self.fail = dict(fail or {})
        self.calls: list[tuple[list[str], dict[str, str], Path | None]] = []
        self.now = 0.0
        self.seconds = {"up": 50.0, "destroy": 20.0}
        self.up_output = "+ gcp:dns:ResponsePolicy policy creating (0s)\n"

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        stderr_to: Path | None = None,
    ) -> probe.Done:
        probe.guard(argv, env)
        self.calls.append((list(argv), dict(env), stderr_to))
        word = next((w for w in ("up", "destroy", "init") if w in argv[:3]), "")
        self.now += self.seconds.get(word, 0.0)
        if argv[:2] == ["pulumi", "version"]:
            return probe.Done(0, "v3.263.0\n", "")
        if argv[:3] == ["pulumi", "plugin", "ls"]:
            return probe.Done(0, "NAME  KIND  VERSION\ngcp   resource  9.37.0  1 MB\n", "")
        outcome = self.fail.get(word, 0)
        if isinstance(outcome, BaseException):
            raise outcome
        if stderr_to is not None and word == "up":
            stderr_to.parent.mkdir(parents=True, exist_ok=True)
            stderr_to.write_text(
                "I1006 05:00:00.000000 1 x.go:1] Provider[gcp, 0x1].Create(urn:pulumi:probe::"
                "ssc-exp091p::gcp:dns/responsePolicyRule:ResponsePolicyRule::ssc-exp091p-0) "
                "executing (#props=5)\n"
                "I1006 05:00:03.300000 1 x.go:1] Provider[gcp, 0x1].Create(urn:pulumi:probe::"
                "ssc-exp091p::gcp:dns/responsePolicyRule:ResponsePolicyRule::ssc-exp091p-0) "
                "success; #outs=9\n"
            )
        out = self.up_output if word == "up" else ""
        err = "boom" if outcome and stderr_to is None else ""  # stderr is in the log file
        return probe.Done(outcome, out, err)

    def commands(self) -> list[str]:
        return [" ".join(c[0]) for c in self.calls]


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    made: list[Path] = []

    def make_dir() -> Path:
        folder = tmp_path / f"backend-{len(made)}"
        folder.mkdir()
        made.append(folder)
        return folder

    return {"tmp": tmp_path, "made": made, "make_dir": make_dir}


def run_main(
    world: dict[str, Any], sh: FakeShell, *argv: str, env: Mapping[str, str] | None = None
) -> tuple[int, list[str], dict[str, Any] | None]:
    said: list[str] = []
    lock = world["tmp"] / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "pulumi"\nversion = "3.263.0"\n\n'
        '[[package]]\nname = "pulumi-gcp"\nversion = "9.37.0"\n'
    )
    results = world["tmp"] / "results.json"
    code = probe.main(
        list(argv),
        sh=sh,
        clock=lambda: sh.now,
        token_source=lambda: TOKEN,
        say=said.append,
        base_env=env if env is not None else {"PATH": "/usr/bin", "PULUMI_ACCESS_TOKEN": "pat"},
        results=results,
        log=world["tmp"] / "logs" / "p8.log",
        lock=lock,
        make_dir=world["make_dir"],
    )
    return code, said, json.loads(results.read_text()) if results.exists() else None


# Arguments, environment and refusals.


def test_up_and_destroy_are_timed_commands_with_the_parallelism_given() -> None:
    assert probe.up_args(32, debug=False) == [
        "pulumi", "up", "--yes", "--skip-preview", "--parallel", "32", "--stack", "probe",
        "--non-interactive",
    ]  # fmt: skip
    assert probe.destroy_args(1, debug=False)[:7] == [
        "pulumi", "destroy", "--yes", "--skip-preview", "--parallel", "1", "--stack",
    ]  # fmt: skip
    assert probe.up_args(8, debug=True)[-3:] == ["--logflow", "-v=9", "--logtostderr"]
    assert "--logflow" not in probe.up_args(8, debug=False)


def test_the_environment_is_a_fresh_file_backend_with_an_empty_passphrase(tmp_path: Path) -> None:
    base = {
        "PATH": "/bin",
        "PULUMI_ACCESS_TOKEN": "pat",
        "PULUMI_BACKEND_URL": "gs://x",
        "HOME": "/h",
    }
    env = probe.environment(base, tmp_path, TOKEN, debug=False)
    assert env["PULUMI_BACKEND_URL"] == f"file://{tmp_path}"
    assert env["PULUMI_CONFIG_PASSPHRASE"] == ""
    assert "PULUMI_ACCESS_TOKEN" not in env
    assert env["GOOGLE_OAUTH_ACCESS_TOKEN"] == TOKEN
    assert env["PATH"] == "/bin"
    assert "TF_LOG" not in env
    assert probe.environment(base, tmp_path, TOKEN, debug=True)["TF_LOG"] == "DEBUG"


def test_any_project_but_the_platform_one_is_refused() -> None:
    assert probe.check_project("ssc-platform-0") == "ssc-platform-0"
    for other in ("ristretto-506621", "ssc-c-proofcell01", ""):
        with pytest.raises(probe.ProbeError, match="only touches ssc-platform-0"):
            probe.check_project(other)
        with pytest.raises(ValueError, match="only touches ssc-platform-0"):
            program.check_project(other)


def test_the_forbidden_project_never_reaches_a_command() -> None:
    with pytest.raises(probe.ProbeError, match="ristretto-506621"):
        probe.guard(["pulumi", "config", "set", "project", "ristretto-506621"], {})
    with pytest.raises(probe.ProbeError, match="ristretto-506621"):
        probe.guard(["pulumi"], {"GOOGLE_CLOUD_PROJECT": "ristretto-506621"})


def test_the_runner_and_the_program_agree_on_the_project() -> None:
    assert probe.PROJECT == program.PROJECT == naming.BOOTSTRAP_PROJECT
    assert probe.FORBIDDEN_PROJECT == program.FORBIDDEN_PROJECT


def test_a_wrong_count_or_parallelism_is_refused_before_anything_runs(
    world: dict[str, Any],
) -> None:
    for argv in (
        ["--parallel", "0"],
        ["--parallel", "a,b"],
        ["--parallel", "8,8"],
        ["--count", "0"],
    ):
        sh = FakeShell()
        code, said, results = run_main(world, sh, *argv)
        assert code == 2
        assert sh.calls == []
        assert results is None
        assert said


def test_the_dry_run_prints_every_command_and_runs_none(world: dict[str, Any]) -> None:
    sh = FakeShell()
    code, said, results = run_main(world, sh, "--dry-run", "--parallel", "1,8,32", "--count", "7")
    assert (code, sh.calls, results) == (0, [], None)
    text = "\n".join(said)
    assert (
        "$ pulumi up --yes --skip-preview --parallel 8 --stack probe --non-interactive --logflow"
        in text
    )
    assert "$ pulumi config set count 7 --stack probe --non-interactive" in text
    assert text.count("$ pulumi destroy") == 3
    assert "P=8" in text and "TF_LOG=DEBUG" in text
    assert TOKEN not in text


# The bucket backend, the padding and skipped checkpoints.

BUCKET_STAMP = datetime(2026, 10, 6, 14, 30, 5, tzinfo=UTC)
PREFIX = "gs://ssc-platform-0-pulumi/exp091p/"


def run_gs(
    world: dict[str, Any], sh: FakeShell, *argv: str
) -> tuple[int, list[str], dict[str, Any] | None]:
    said: list[str] = []
    lock = world["tmp"] / "uv.lock"
    lock.write_text('[[package]]\nname = "pulumi"\nversion = "3.263.0"\n')
    results = world["tmp"] / "results.json"
    code = probe.main(
        list(argv), sh=sh, clock=lambda: sh.now, token_source=lambda: TOKEN, say=said.append,
        base_env={"PATH": "/usr/bin"}, results=results, log=world["tmp"] / "logs" / "p8.log",
        lock=lock, make_dir=world["make_dir"], utcnow=lambda: BUCKET_STAMP,
    )  # fmt: skip
    return code, said, json.loads(results.read_text()) if results.exists() else None


def test_the_gs_backend_is_a_fresh_prefix_in_the_platform_bucket_for_each_p(
    world: dict[str, Any],
) -> None:
    sh = FakeShell()
    code, said, results = run_gs(world, sh, "--parallel", "1,32", "--backend", "gs")
    assert code == 0
    assert results is not None
    urls = {c[1]["PULUMI_BACKEND_URL"] for c in sh.calls if c[0][1:2] == ["up"]}
    assert urls == {f"{PREFIX}1-20261006T143005Z", f"{PREFIX}32-20261006T143005Z"}
    assert world["made"] == []  # no temporary folder
    assert {r["backend_url"] for r in results["runs"]} == urls
    assert f"P=1: state in {PREFIX}1-20261006T143005Z" in said  # printed, for leftovers


def test_the_stack_is_removed_after_a_clean_destroy_on_the_bucket_only(
    world: dict[str, Any],
) -> None:
    sh = FakeShell()
    run_gs(world, sh, "--parallel", "1", "--backend", "gs")
    commands = sh.commands()
    rm = "pulumi stack rm --yes --force --stack probe --non-interactive"
    assert commands[-1] == rm
    assert commands.index(rm) > next(i for i, c in enumerate(commands) if " destroy " in c)
    assert sh.calls[-1][1]["PULUMI_BACKEND_URL"].startswith(PREFIX)
    assert "PULUMI_SKIP_CHECKPOINTS" not in sh.calls[-1][1]
    file_sh = FakeShell()
    run_gs(world, file_sh, "--parallel", "1")
    assert not any(" rm " in c for c in file_sh.commands())  # the temporary folder is removed


def test_a_failed_destroy_keeps_the_bucket_state_and_prints_its_prefix(
    world: dict[str, Any],
) -> None:
    sh = FakeShell({"destroy": 1})
    code, said, results = run_gs(world, sh, "--parallel", "1", "--backend", "gs")
    assert code == 1
    assert results is not None
    assert not any(" rm " in c for c in sh.commands())  # --force would orphan the resources
    assert f"{PREFIX}1-20261006T143005Z" in results["runs"][0]["error"]
    assert any("DESTROY FAILED" in line and PREFIX in line for line in said)


def test_any_other_bucket_is_refused_before_a_command_runs() -> None:
    for url in (
        "gs://ristretto-state/exp091p/1-x",
        "gs://ssc-platform-0-pulumi/",
        "gs://ssc-platform-0-pulumi/exp091p/",
        "gs://ssc-platform-0-pulumi/other/1-x",
        "gs://ssc-platform-0-pulumi/exp091p/../prod",
        "gs://ssc-platform-0-pulumi-2/exp091p/1-x",
        "s3://ssc-platform-0-pulumi/exp091p/1-x",
    ):
        with pytest.raises(probe.ProbeError, match="refusing backend"):
            probe.check_backend(url)
        with pytest.raises(probe.ProbeError, match="refusing backend"):
            probe.guard(["pulumi", "up"], {"PULUMI_BACKEND_URL": url})
    assert probe.check_backend(f"{PREFIX}8-20261006T143005Z")
    assert probe.check_backend("file:///tmp/x")
    assert probe.backend_url(8, BUCKET_STAMP) == f"{PREFIX}8-20261006T143005Z"


def test_skip_checkpoints_is_set_for_the_up_and_the_destroy_only(world: dict[str, Any]) -> None:
    sh = FakeShell()
    run_gs(world, sh, "--parallel", "1", "--skip-checkpoints")
    skipping = {c[0][1]: c[1].get("PULUMI_SKIP_CHECKPOINTS") for c in sh.calls if len(c[0]) > 1}
    assert skipping["up"] == skipping["destroy"] == "true"
    assert skipping["config"] is None and skipping["stack"] is None
    plain = FakeShell()
    run_gs(world, plain, "--parallel", "1")
    assert not any("PULUMI_SKIP_CHECKPOINTS" in c[1] for c in plain.calls)


def test_the_padding_is_set_as_config_for_every_run_and_recorded(world: dict[str, Any]) -> None:
    sh = FakeShell()
    code, said, results = run_gs(
        world, sh, "--parallel", "1", "--pad-mb", "7", "--backend", "gs", "--skip-checkpoints"
    )
    assert code == 0
    assert results is not None
    assert "pulumi config set pad_mb 7 --stack probe --non-interactive" in sh.commands()
    run = results["runs"][0]
    assert (run["backend"], run["pad_mb"], run["skip_checkpoints"]) == ("gs", 7, True)
    plain = FakeShell()
    _, _, results = run_gs(world, plain, "--parallel", "1")
    assert results is not None
    assert "pulumi config set pad_mb 0 --stack probe --non-interactive" in plain.commands()
    run = results["runs"][0]
    assert (run["backend"], run["pad_mb"], run["skip_checkpoints"]) == ("file", 0, False)


def test_a_pad_outside_zero_to_ten_is_refused_before_anything_runs(world: dict[str, Any]) -> None:
    for pad in ("-1", "11"):
        sh = FakeShell()
        code, said, results = run_gs(world, sh, "--pad-mb", pad)
        assert (code, sh.calls, results) == (2, [], None)
        assert "--pad-mb is 0 to 10" in said[0]


def test_the_dry_run_shows_the_bucket_prefix_the_padding_and_the_stack_rm(
    world: dict[str, Any],
) -> None:
    sh = FakeShell()
    code, said, _ = run_gs(
        world, sh, "--dry-run", "--backend", "gs", "--pad-mb", "3", "--skip-checkpoints"
    )
    text = "\n".join(said)
    assert code == 0 and sh.calls == []
    assert "PULUMI_BACKEND_URL=gs://ssc-platform-0-pulumi/exp091p/<P>-<UTC timestamp>" in text
    assert "PULUMI_SKIP_CHECKPOINTS=true" in text and "padded with 3 MB" in text
    assert "$ pulumi config set pad_mb 3" in text
    assert "$ pulumi stack rm --yes --force" in text


# The runs, the destroy in finally, and the results.


def test_each_p_gets_a_fresh_backend_and_a_timed_up_and_destroy(world: dict[str, Any]) -> None:
    sh = FakeShell()
    code, said, results = run_main(world, sh, "--parallel", "1,32", "--count", "99")
    assert code == 0
    assert results is not None
    assert [r["parallel"] for r in results["runs"]] == [1, 32]
    first = results["runs"][0]
    assert (first["up_seconds"], first["destroy_seconds"]) == (50.0, 20.0)
    assert first["up_rules_per_minute"] == 120.0  # 100 rules in 50 s
    assert first["destroy_rules_per_minute"] == 300.0
    assert first["up_ok"] and first["destroy_ok"] and "error" not in first
    backends = {c[1]["PULUMI_BACKEND_URL"] for c in sh.calls if "up" in c[0][:2]}
    assert backends == {f"file://{p}" for p in world["made"]}
    assert len(world["made"]) == 2
    assert not any(p.exists() for p in world["made"])  # removed after a clean destroy
    assert all(c[1]["GOOGLE_OAUTH_ACCESS_TOKEN"] == TOKEN for c in sh.calls if c[0][1:2] == ["up"])
    assert results["pulumi_cli"] == "v3.263.0"
    assert (results["pulumi_gcp"], results["pulumi_sdk"]) == ("9.37.0", "3.263.0")
    assert results["gcp_plugin"] == "gcp resource 9.37.0"
    assert "pulumi v3.263.0; pulumi-gcp 9.37.0; plugin gcp resource 9.37.0" in said[0]
    assert not any("PULUMI_ACCESS_TOKEN" in c[1] for c in sh.calls)


def test_the_token_comes_from_the_environment_before_gcloud(world: dict[str, Any]) -> None:
    sh = FakeShell()
    run_main(world, sh, "--parallel", "1", env={"GOOGLE_OAUTH_ACCESS_TOKEN": "from-env"})
    assert {c[1]["GOOGLE_OAUTH_ACCESS_TOKEN"] for c in sh.calls if c[0][1:2] == ["up"]} == {
        "from-env"
    }


def test_only_the_p8_run_keeps_the_debug_log(world: dict[str, Any]) -> None:
    sh = FakeShell()
    code, said, results = run_main(world, sh, "--parallel", "1,8")
    assert code == 0
    assert results is not None
    by_p = {c[0][5]: c for c in sh.calls if c[0][1:2] == ["up"]}
    assert by_p["1"][2] is None and "TF_LOG" not in by_p["1"][1]
    assert by_p["8"][2] == world["tmp"] / "logs" / "p8.log"
    assert by_p["8"][1]["TF_LOG"] == "DEBUG"
    assert "-v=9" in by_p["8"][0]
    assert results["runs"][1]["log"].endswith("p8.log")
    assert any("rule creates: 1 started, 1 ended" in line for line in said)


def test_the_destroy_runs_when_the_up_fails(world: dict[str, Any]) -> None:
    sh = FakeShell({"up": 1})
    code, said, results = run_main(world, sh, "--parallel", "1,8")
    assert code == 1
    assert results is not None
    run = results["runs"][0]
    assert run["up_ok"] is False
    assert "pulumi up exited 1" in run["error"]
    assert run["destroy_ok"] is True
    destroys = [c for c in sh.commands() if " destroy " in c]
    assert len(destroys) == 2  # it had begun creating, so the next P still ran
    assert [r["parallel"] for r in results["runs"]] == [1, 8]
    assert "up_rules_per_minute" not in run  # a failed up has no rate
    assert "startup_failure" not in run


def test_a_startup_failure_stops_after_the_first_p(world: dict[str, Any]) -> None:
    """The first run failed in the language host before any resource: the same error three times."""
    sh = FakeShell({"up": 1})
    sh.up_output = ""  # no "creating" line: pulumi never got to a resource
    code, said, results = run_main(world, sh, "--parallel", "1,8,32")
    assert code == 1
    assert results is not None
    assert [r["parallel"] for r in results["runs"]] == [1]
    assert results["runs"][0]["startup_failure"] is True
    assert sum(" up " in c for c in sh.commands()) == 1
    assert sum(" destroy " in c for c in sh.commands()) == 1  # the empty stack is still destroyed
    assert any("stopping after P=1" in line for line in said)


def test_the_error_of_a_logged_run_is_read_from_its_log(world: dict[str, Any]) -> None:
    sh = FakeShell({"up": 1})
    sh.up_output = ""
    code, said, results = run_main(world, sh, "--parallel", "8")
    assert results is not None
    assert "Create(urn:pulumi" in results["runs"][0]["error"]  # the fake wrote the log, not stderr


def test_the_destroy_runs_when_the_run_is_interrupted(world: dict[str, Any]) -> None:
    sh = FakeShell({"up": KeyboardInterrupt()})
    with pytest.raises(KeyboardInterrupt):
        run_main(world, sh, "--parallel", "1,8")
    assert sum(" destroy " in c for c in sh.commands()) == 1
    assert not sh.commands()[-1].endswith("up")  # the destroy came after the interrupted up
    assert (world["tmp"] / "results.json").exists()


def test_no_destroy_when_no_stack_was_made(world: dict[str, Any]) -> None:
    sh = FakeShell({"init": 1})
    code, said, results = run_main(world, sh, "--parallel", "1")
    assert code == 1
    assert results is not None
    assert not any(" destroy " in c or " up " in c for c in sh.commands())
    assert "stack init" in results["runs"][0]["error"]


def test_a_destroy_that_fails_keeps_the_state_and_says_how_to_clean_up(
    world: dict[str, Any],
) -> None:
    sh = FakeShell({"destroy": 1})
    code, said, results = run_main(world, sh, "--parallel", "1")
    assert code == 1
    assert results is not None
    (backend,) = world["made"]
    assert backend.exists()
    assert str(backend) in results["runs"][0]["error"]
    assert "sinkhole.py --cleanup-only" in results["runs"][0]["error"]
    assert any("DESTROY FAILED" in line for line in said)


def test_the_runner_refuses_a_missing_lock_file(world: dict[str, Any]) -> None:
    said: list[str] = []
    sh = FakeShell()
    code = probe.main(
        ["--parallel", "1"], sh=sh, say=said.append, lock=world["tmp"] / "missing",
        base_env={}, results=world["tmp"] / "r.json",
    )  # fmt: skip
    assert code == 2
    assert "uv.lock" in said[0] or "missing" in said[0]
    assert sh.calls == []


# The log reader.

LOG = """\
I1006 05:00:00.000000    1 provider_plugin.go:1] Provider[gcp, 0x1].Create(urn:pulumi:probe::ssc-exp091p::gcp:dns/responsePolicyRule:ResponsePolicyRule::ssc-exp091p-0) executing (#props=5)
I1006 05:00:00.010000    1 provider_plugin.go:1] Provider[gcp, 0x1].Create(urn:pulumi:probe::ssc-exp091p::gcp:dns/responsePolicyRule:ResponsePolicyRule::ssc-exp091p-1) executing (#props=5)
2026-10-06T05:00:00.500Z [DEBUG] provider.terraform-provider-gcp: Google API Request Details:
---[ REQUEST ]---
POST /dns/v1beta2/projects/ssc-platform-0/locations/global/responsePolicies/ssc-exp091p/rules?alt=json HTTP/1.1
Host: dns.googleapis.com

---[ RESPONSE ]---
HTTP/2.0 200 OK
I1006 05:00:00.600000    1 provider_plugin.go:1] Provider[gcp, 0x1].Create(urn:pulumi:probe::ssc-exp091p::gcp:dns/responsePolicyRule:ResponsePolicyRule::ssc-exp091p-0) success; #outs=9
2026-10-06T05:00:03.500Z [DEBUG] provider.terraform-provider-gcp: Google API Request Details:
---[ REQUEST ]---
GET /dns/v1beta2/projects/ssc-platform-0/responsePolicies/ssc-exp091p/rules/ssc-exp091p-0?alt=json HTTP/1.1

---[ RESPONSE ]---
HTTP/2.0 200 OK
2026-10-06T05:00:04.000Z [DEBUG] provider.terraform-provider-gcp: Retry Transport: waiting 1s after 429 Too Many Requests
I1006 05:00:06.000000    1 provider_plugin.go:1] Provider[gcp, 0x1].Create(urn:pulumi:probe::ssc-exp091p::gcp:dns/responsePolicyRule:ResponsePolicyRule::ssc-exp091p-1) failed: boom
I1006 05:00:06.100000    1 provider_plugin.go:1] Provider[gcp, 0x1].Create(urn:pulumi:probe::ssc-exp091p::gcp:dns/responsePolicy:ResponsePolicy::policy) executing (#props=2)
"""


def test_the_log_reader_pairs_creates_and_http_calls_and_finds_the_retries() -> None:
    summary = logsummary.summarise(LOG.splitlines())
    assert [(c.name, round(c.seconds or 0, 2), c.outcome) for c in summary.creates] == [
        ("ssc-exp091p-0", 0.6, "success"),
        ("ssc-exp091p-1", 5.99, "failed"),
    ]  # the policy's own create is not a rule's
    assert logsummary.in_flight(summary.creates) == 2
    assert [(c.method, logsummary.kind(c.path), c.status) for c in summary.calls] == [
        ("POST", "/dns/v1beta2/projects/ssc-platform-0/locations/global/responsePolicies/ssc-exp091p/rules", "200"),
        ("GET", "/dns/v1beta2/projects/ssc-platform-0/responsePolicies/ssc-exp091p/rules/{rule}", "200"),
    ]  # fmt: skip
    assert summary.calls[0].at - summary.origin == pytest.approx(0.5)
    assert summary.calls[1].at - summary.origin == pytest.approx(3.5)
    assert len(summary.trouble) == 1
    assert "429 Too Many Requests" in summary.trouble[0]


def test_the_report_names_the_numbers_a_person_reads() -> None:
    text = "\n".join(logsummary.report(logsummary.summarise(LOG.splitlines())))
    assert "rule creates: 2 started, 2 ended, 1 failed, up to 2 at once" in text
    assert "ssc-exp091p-0" in text
    assert (
        "POST /dns/v1beta2/projects/ssc-platform-0/locations/global/responsePolicies/ssc-exp091p/rules -> 200"
        in text
    )
    assert "429, retry, sleep, backoff or quota lines: 1" in text


def test_a_log_without_creates_says_so() -> None:
    text = "\n".join(logsummary.report(logsummary.summarise(["nothing here", ""])))
    assert "no ResponsePolicyRule Create lines found" in text


def test_the_clock_going_past_midnight_keeps_counting() -> None:
    lines = [
        "I1006 23:59:58.000000 1 x] Provider[gcp, 0x1].Create(urn:pulumi:p::q::gcp:dns/responsePolicyRule:ResponsePolicyRule::r0) executing",
        "I1007 00:00:02.000000 1 x] Provider[gcp, 0x1].Create(urn:pulumi:p::q::gcp:dns/responsePolicyRule:ResponsePolicyRule::r0) success",
    ]  # fmt: skip
    (create,) = logsummary.summarise(lines).creates
    assert create.seconds == pytest.approx(4.0)


# The program.


def declare(
    count: int, project: str = program.PROJECT, pad_mb: int = 0
) -> list[mockcloud.Declared]:
    recorder = mockcloud.Recorder()
    asyncio.set_event_loop(asyncio.new_event_loop())
    pulumi.runtime.set_mocks(recorder, project="ssc-exp091p", stack="probe", preview=False)

    @pulumi.runtime.test
    def build() -> None:
        program.build(project, count, pad_mb)

    build()
    return recorder.declared


RULE = "gcp:dns/responsePolicyRule:ResponsePolicyRule"


def test_the_program_declares_a_policy_a_sinkhole_rule_and_n_rules_named_ssc_exp091p() -> None:
    declared = declare(5)
    policy = [d for d in declared if d.type == "gcp:dns/responsePolicy:ResponsePolicy"]
    assert [(d.inputs["responsePolicyName"], d.inputs["project"]) for d in policy] == [
        ("ssc-exp091p", "ssc-platform-0")
    ]
    assert "networks" not in policy[0].inputs
    rules = [d for d in declared if d.type == RULE]
    assert sorted(d.name for d in rules) == sorted(
        ["ssc-exp091p-sink", *(f"ssc-exp091p-{i}" for i in range(5))]
    )
    assert all(d.inputs["ruleName"].startswith("ssc-exp091") for d in rules)
    assert all(d.inputs["project"] == "ssc-platform-0" for d in rules)


def test_the_program_refuses_other_projects_and_empty_counts() -> None:
    with pytest.raises(ValueError, match="only touches ssc-platform-0"):
        program.build("ristretto-506621", 5)
    with pytest.raises(ValueError, match="at least 1"):
        program.build(program.PROJECT, 0)


def _shape(inputs: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """A rule's inputs with the names that differ by design replaced, for comparison. The project
    is an output in the cell and a string here, and the policy's name differs."""
    text = json.dumps(inputs, sort_keys=True, default=str)
    for old, new in names.items():
        text = text.replace(old, new)
    out: dict[str, Any] = json.loads(text)
    out.pop("project", None)
    out.pop("responsePolicy", None)
    return out


def test_a_rule_has_exactly_the_inputs_the_cell_gives_one_sinkhole_rule() -> None:
    """Copied, not imported: this test is where the copy is held to the cell's code."""
    cell_rules = {
        d.name: d.inputs
        for d in mockcloud.run(naming.cell_stack("probecmp01"), {"stage": "staging"})
        if d.type == RULE
    }
    probe_rules = {d.name: d.inputs for d in declare(3) if d.type == RULE}
    tld = _shape(cell_rules["dns-tld-com"], {"*.com.": "N", "tld-com": "R"})
    mine = _shape(probe_rules["ssc-exp091p-1"], {"*.ssc-exp091p-1.": "N", "ssc-exp091p-1": "R"})
    assert mine == tld
    sink = _shape(cell_rules["dns-sinkhole"], {})
    mine_sink = _shape(probe_rules["ssc-exp091p-sink"], {})
    assert {k: v for k, v in mine_sink.items() if k != "ruleName"} == {
        k: v for k, v in sink.items() if k != "ruleName"
    }
    assert program.SINKHOLE_NAME in json.dumps(mine)


def test_the_padding_is_one_incompressible_component_output_of_that_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = program.pad_text(2)
    assert len(text) == 2 * 1024 * 1024
    assert text == program.pad_text(2)  # the same every run
    assert len(zlib.compress(text.encode())) > len(text) * 0.45  # hex: about half at best
    seen: list[dict[str, Any]] = []
    register = pulumi.ComponentResource.register_outputs

    def spy(self: pulumi.ComponentResource, outputs: Any = None) -> None:
        seen.append(dict(outputs))
        register(self, outputs)

    monkeypatch.setattr(pulumi.ComponentResource, "register_outputs", spy)
    declared = declare(2, pad_mb=2)
    assert [len(o["pad"]) for o in seen] == [2 * 1024 * 1024]
    assert [d.name for d in declared if d.type == "ssc:probe:Pad"] == ["pad"]
    plain = declare(2)
    assert not [d for d in plain if d.type == "ssc:probe:Pad"]  # no padding by default
    assert len([d for d in declared if d.type == RULE]) == len([d for d in plain if d.type == RULE])


def test_the_padding_is_in_the_state_before_the_first_resource_it_slows() -> None:
    policy = next(
        d for d in declare(1, pad_mb=1) if d.type == "gcp:dns/responsePolicy:ResponsePolicy"
    )
    assert policy.name == "policy"


def test_the_program_refuses_a_pad_outside_zero_to_ten() -> None:
    for pad in (-1, 11):
        with pytest.raises(ValueError, match="pad_mb is 0 to 10"):
            program.build(program.PROJECT, 1, pad)
