"""``ssc_infra.onboard`` runs the steps in order, times them, and says where to resume. Nothing
here runs a real command: Pulumi, the shell, the clock and ``pulumi up`` are fakes."""

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from ssc_infra import naming as n
from ssc_infra import onboard
from ssc_infra.onboard import Counts, Done, Onboarding, Options, Tools
from ssc_infra.run import CommandError

LABEL = "onbcell01"
STACK = n.cell_stack(LABEL)
PROJECT = n.cell_project(LABEL)
GATEWAY_IMAGE = f"{n.platform_registry()}/ssc-gateway@sha256:" + "a" * 64
PROBE_DIGEST = "sha256:" + "b" * 64
PROBE_IMAGE = f"{n.platform_registry()}/ssc-probe@{PROBE_DIGEST}"
SETTINGS = {
    "gateway_image": GATEWAY_IMAGE,
    "org_id": "org_" + "c" * 20,
    "datagw_image": f"{n.platform_registry()}/ssc-datagw@sha256:" + "d" * 64,
    "oncall_email": "ops@example.com",
}
KMS_KEY = "projects/p/locations/l/keyRings/r/cryptoKeys/gateway"
KEYRING = b"PLAIN-KEYRING-MUST-NOT-APPEAR"
SEALED = b"SEALED-BYTES"
ARGV = [LABEL, "--probe-image", PROBE_IMAGE]


class World:
    """The fake machine, with a clock that only the fakes move."""

    def __init__(self, tmp_path: Path) -> None:
        self.now = 1000.0
        self.pulumi_calls: list[tuple[str, ...]] = []
        self.shell_calls: list[tuple[str, ...]] = []
        self.shell_envs: list[Mapping[str, str] | None] = []
        self.applies: list[str] = []
        self.stacks: list[str] = []
        self.outputs: dict[str, str] = {}
        self.config: dict[str, str] = {}
        self.applied_config: dict[str, str] | None = None
        self.out: list[str] = []
        self.err: list[str] = []
        self.cost: dict[str, float] = {}
        self.apply_seconds = 60.0
        self.cert_states: list[str] = ["PROVISIONING", "PROVISIONING", "ACTIVE"]
        self.cert_calls = 0
        self.image_in_registry = False
        self.probe_code = 0
        self.fail: Callable[[Sequence[str]], bool] = lambda _argv: False
        self.tmp_path = tmp_path

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def pulumi(self, *args: str, cwd: str | None = None) -> str:
        self.pulumi_calls.append(args)
        if self.fail(args):
            raise CommandError("pulumi " + " ".join(args[:2]) + " failed: boom")
        if args[:2] == ("stack", "ls"):
            return json.dumps([{"name": s} for s in self.stacks])
        if args[:2] == ("stack", "init"):
            self.stacks.append(args[2])
        if args[:2] == ("stack", "output"):
            if "--json" in args:
                if self.applied_config is None:
                    raise CommandError("pulumi stack output failed: no such output")
                return json.dumps(self.applied_config)
            name = args[-1]
            if name not in self.outputs:
                raise CommandError("pulumi stack output failed: no such output")
            return self.outputs[name] + "\n"
        if args[:2] == ("config", "get"):
            if args[-1] not in self.config:
                raise CommandError("pulumi config get failed: missing")
            return self.config[args[-1]] + "\n"
        if args[:2] == ("config", "set"):
            self.config[args[-2]] = args[-1]
        if args[:2] == ("config", "set-all"):
            for flag, pair in zip(args, args[1:], strict=False):
                if flag == "--plaintext":
                    key, _, value = pair.partition("=")
                    self.config[key] = value
        return ""

    def shell(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,  # noqa: A002
    ) -> Done:
        argv = tuple(argv)
        self.shell_calls.append(argv)
        self.shell_envs.append(env)
        self.now += self.cost.get(argv[0], 1.0)
        if self.fail(argv):
            return Done(1, b"", "docker: boom")
        line = " ".join(argv)
        if line.endswith("ssc_edge.keys new"):
            return Done(0, KEYRING, "")
        if line.endswith("ssc_edge.keys jwks"):
            assert input == KEYRING
            return Done(0, b'{"keys": []}\n', "")
        if argv[:3] == ("gcloud", "kms", "encrypt"):
            assert input == KEYRING
            return Done(0, SEALED, "")
        if argv[:4] == ("gcloud", "artifacts", "docker", "images"):
            return Done(0 if self.image_in_registry else 1, b"", "")
        if argv[:2] == ("gcloud", "certificate-manager"):
            state = self.cert_states[min(self.cert_calls, len(self.cert_states) - 1)]
            self.cert_calls += 1
            return Done(0, state.encode() + b"\n", "")
        if argv[-1] == "ssc_conformance.nightly":
            return Done(self.probe_code, b"floor probes: 12 passed\n", "")
        return Done(0, b"", "")

    def apply(self, stack: str, beat: Callable[[Counts], None]) -> None:
        self.applies.append(stack)
        if self.fail(("apply", stack)):
            raise CommandError("pulumi up failed (full output in /x/up.log):\nerror: boom")
        beat(Counts(started=40, finished=30))
        self.now += self.apply_seconds
        if len(self.applies) == 1:
            self.outputs["gateway_kms_key"] = KMS_KEY
            self.applied_config = {"stage": "staging"}
        else:
            self.applied_config = {
                "gateway_keyring": "x",
                "probe_digest": PROBE_DIGEST,
            }

    def tools(self) -> Tools:
        return Tools(
            pulumi=self.pulumi,
            shell=self.shell,
            apply=self.apply,
            clock=self.clock,
            sleep=self.sleep,
            out=self.out.append,
            err=self.err.append,
            env={"HOME": "/home/x"},
            infra_dir=str(self.tmp_path),
            repo_root=self.tmp_path,
        )

    def run(self, *argv: str) -> int:
        settings = self.tmp_path / "settings.json"
        settings.write_text(json.dumps(SETTINGS))
        return onboard.main([*argv, "--settings", str(settings)], self.tools())

    def commands(self) -> list[str]:
        return [" ".join(a) for a in self.shell_calls]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_each_step_is_numbered_timed_and_the_verdict_counts_all_but_the_certificate(
    world: World,
) -> None:
    assert world.run(*ARGV) == 0
    total = len(onboard.STEP_NAMES)
    for number, name in enumerate(onboard.STEP_NAMES, 1):
        assert any(line.startswith(f"[{number}/{total}] {name}") for line in world.out)
    assert "[2/8] skipped (SSC-091 phase 2)" in world.out
    assert any(line.startswith("[3/8] done in 01:00, total ") for line in world.out)
    assert any("      ... 30 resources done, 10 in flight, 00:00" in x for x in world.out)
    # two applies of 60 s, the shell steps at 1 s each, a certificate wait of two polls (60 s)
    assert world.out[-1].startswith("onboarding 02:")
    assert "(budget 15:00, PASS); certificate 02:" in world.out[-1]
    assert world.err == []


def test_the_verdict_leaves_the_certificate_wait_out_and_says_over_when_it_is(
    world: World,
) -> None:
    world.apply_seconds = 600.0
    world.cert_states = ["PROVISIONING"] * 40 + ["ACTIVE"]  # 20 minutes
    assert world.run(*ARGV) == 0
    verdict_line = world.out[-1]
    assert "(budget 15:00, OVER)" in verdict_line
    assert verdict_line.startswith("onboarding 20:")
    # from the end of the first apply: the second apply, the shell steps and the 20-minute wait
    assert verdict_line.endswith("certificate 30:45")


def test_the_late_settings_wait_for_step_4_and_the_keyring_is_never_printed(
    world: World,
) -> None:
    assert world.run(*ARGV) == 0
    sets = [c for c in world.pulumi_calls if c[:2] == ("config", "set-all")]
    assert len(sets) == 2
    first = " ".join(sets[0])
    for held in onboard.LATE_SETTINGS:
        assert f"{held}=" not in first
    assert "stage=staging" in first
    assert "probe=true" in first
    assert "oncall_email=ops@example.com" in first
    second = " ".join(sets[1])
    assert f"gateway_image={GATEWAY_IMAGE}" in second
    assert "org_id=org_" in second
    assert "datagw_image=" in second
    assert "gateway_keyring=" + onboard.base64.b64encode(SEALED).decode() in second
    assert 'gateway_jwks={"keys": []}' in second
    everything = "\n".join(world.out + world.err + world.commands())
    assert KEYRING.decode() not in everything


def test_the_keyring_is_made_sealed_with_the_stack_s_key_and_the_floor_probes_get_the_cell(
    world: World,
) -> None:
    assert world.run(*ARGV) == 0
    sealing = [c for c in world.shell_calls if c[:3] == ("gcloud", "kms", "encrypt")]
    assert sealing[0][3] == f"--key={KMS_KEY}"
    copy = f"{n.REGION}-docker.pkg.dev/{PROJECT}/ssc-apps/apps:nightly-probe {PROBE_IMAGE}"
    assert f"docker buildx imagetools create --tag {copy}" in world.commands()
    assert world.config["probe_digest"] == PROBE_DIGEST
    env = world.shell_envs[-1]
    assert env is not None
    assert env["SSC_PROBE_PROJECT"] == PROJECT
    assert env["SSC_PROBE_AGENT_URL"] == n.agent_url(LABEL)
    assert env["SSC_PROBE_DIGEST"] == PROBE_DIGEST
    assert env["HOME"] == "/home/x"


def test_the_certificate_is_waited_for_before_the_floor_probes(world: World) -> None:
    assert world.run(*ARGV) == 0
    kinds = [c[1] if c[0] == "gcloud" else c[-1] for c in world.shell_calls]
    assert kinds.index("certificate-manager") < kinds.index("ssc_conformance.nightly")
    assert world.cert_calls == 3


def test_a_failed_certificate_stops_the_run_at_once(world: World) -> None:
    world.cert_states = ["PROVISIONING", "FAILED"]
    assert world.run(*ARGV) == 1
    assert "[7/8] FAILED after 00:32, total " in "\n".join(world.out)
    assert "the certificate FAILED" in world.err[0]
    assert world.err[1].endswith("--from-step 7")


def test_a_certificate_that_stays_pending_gives_up_after_120_minutes(world: World) -> None:
    world.cert_states = ["PROVISIONING"]
    assert world.run(*ARGV) == 1
    assert "PROVISIONING after 120:00" in world.err[0]
    assert world.cert_calls > 200


def test_a_failure_prints_the_step_its_time_and_the_command_to_resume(
    world: World, tmp_path: Path
) -> None:
    world.fail = lambda argv: argv[:3] == ("docker", "buildx", "imagetools")
    world.cost["docker"] = 42.0
    assert world.run(*ARGV) == 1
    assert any(line.startswith("[5/8] FAILED after 00:42, total ") for line in world.out)
    assert "docker buildx imagetools failed (exit 1): docker: boom" in world.err[0]
    assert world.err[1] == (
        f"resume with: uv run python -m ssc_infra.onboard {LABEL} --probe-image {PROBE_IMAGE} "
        f"--settings {tmp_path / 'settings.json'} --from-step 5"
    )
    assert world.out[-1].startswith("[5/8] FAILED")
    assert len(world.applies) == 1


def test_a_failed_pulumi_up_names_step_3(world: World) -> None:
    world.fail = lambda argv: argv == ("apply", STACK)
    assert world.run(*ARGV) == 1
    assert any(line.startswith("[3/8] FAILED") for line in world.out)
    assert "pulumi up failed" in world.err[0]
    assert world.err[1].endswith("--from-step 3")


def test_an_existing_stack_is_refused_without_resume(world: World) -> None:
    world.stacks = [STACK]
    assert world.run(*ARGV) == 1
    assert f"stack {STACK} already exists: pass --resume" in world.err[0]
    assert not any(c[:2] == ("config", "set-all") for c in world.pulumi_calls)
    assert world.applies == []


@pytest.mark.parametrize("label", ["proofcell01", "proofcell02"])
def test_the_proof_run_s_cells_are_refused_before_anything_runs(world: World, label: str) -> None:
    assert world.run(label, "--probe-image", PROBE_IMAGE) == 2
    assert "live proof-run cell" in world.err[0]
    assert world.pulumi_calls == []
    assert world.shell_calls == []
    assert world.out == []


@pytest.mark.parametrize("label", ["proofcell01", "proofcell02"])
def test_the_dry_run_refuses_them_too(world: World, label: str) -> None:
    assert world.run(label, "--probe-image", PROBE_IMAGE, "--dry-run") == 2
    assert world.out == []


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([LABEL, "--probe-image", "ubuntu:latest"], "--probe-image must be"),
        (["Bad_Label", "--probe-image", PROBE_IMAGE], "cell label"),
        ([*ARGV, "--set", "gateway_keyring=x"], "are not taken here"),
        ([*ARGV, "--set", "probe_digest=x"], "are not taken here"),
        ([*ARGV, "--set", "stage=production"], "are not taken here"),
        ([*ARGV, "--set", "org_id=org_short"], "org_id must be"),
        ([*ARGV, "--set", "gateway_image=docker.io/x@sha256:" + "a" * 64], "gateway_image must"),
        ([*ARGV, "--set", "nonsense"], "KEY=VALUE"),
        ([*ARGV, "--from-step", "9"], "--from-step is 1 to 8"),
    ],
)
def test_bad_arguments_are_refused_before_anything_runs(
    world: World, argv: list[str], message: str
) -> None:
    assert world.run(*argv) == 2
    assert message in world.err[0]
    assert world.pulumi_calls == []
    assert world.shell_calls == []


def test_the_settings_must_name_the_gateway_image_and_the_org(world: World, tmp_path: Path) -> None:
    only = tmp_path / "only.json"
    only.write_text(json.dumps({"oncall_email": "a@b.c"}))
    assert onboard.main([*ARGV, "--settings", str(only)], world.tools()) == 2
    assert "must include gateway_image, org_id" in world.err[0]


def test_the_dry_run_prints_every_step_with_its_commands_and_runs_none(world: World) -> None:
    assert world.run(*ARGV, "--dry-run") == 0
    assert world.pulumi_calls == []
    assert world.shell_calls == []
    text = "\n".join(world.out)
    for number, name in enumerate(onboard.STEP_NAMES, 1):
        assert f"[{number}/8] {name}" in text
    assert f"$ pulumi stack init {STACK} --secrets-provider={n.SECRETS_PROVIDER}" in text
    assert f"$ pulumi up --stack {STACK} --yes --skip-preview --non-interactive --event-log" in text
    assert f"docker buildx imagetools create --tag {n.REGION}-docker.pkg.dev/{PROJECT}" in text
    assert PROBE_IMAGE in text
    assert f"--project={PROJECT} --location=global" in text
    assert f"SSC_PROBE_AGENT_URL={n.agent_url(LABEL)}" in text
    assert "skipped (SSC-091 phase 2)" in text
    assert "gateway_keyring=<sealed keyring>" in text
    assert "gateway_image=" not in text.split("[4/8]")[0]


def test_from_step_skips_the_earlier_steps_and_marks_the_verdict_as_this_run_only(
    world: World,
) -> None:
    world.stacks = [STACK]
    world.outputs["gateway_kms_key"] = KMS_KEY
    world.config["gateway_keyring"] = "set"
    world.config["probe_digest"] = PROBE_DIGEST
    world.image_in_registry = True
    assert world.run(*ARGV, "--from-step", "6") == 0
    assert "[1/8] stack and settings: skipped (--from-step 6)" in world.out
    assert not any(c[:2] == ("stack", "init") for c in world.pulumi_calls)
    assert world.applies == [STACK]
    assert world.out[-1].endswith("[resumed: this run only]")
    assert "certificate not measured (resumed after the first apply)" in world.out[-1]


def test_resume_skips_the_work_it_finds_done(world: World) -> None:
    world.stacks = [STACK]
    world.outputs["gateway_kms_key"] = KMS_KEY
    world.config["gateway_keyring"] = "set"
    world.config["probe_digest"] = PROBE_DIGEST
    world.image_in_registry = True
    world.applied_config = {"gateway_keyring": "x", "probe_digest": PROBE_DIGEST}
    world.cert_states = ["ACTIVE"]
    assert world.run(*ARGV, "--resume") == 0
    assert world.applies == []
    skipped = [line for line in world.out if "skipped (" in line]
    assert len(skipped) == 5
    assert not any("imagetools" in c for c in world.commands())
    assert world.out[-1].endswith("certificate already active [resumed: this run only]")


def test_resume_picks_up_where_the_first_apply_finished(world: World) -> None:
    world.stacks = [STACK]
    world.outputs["gateway_kms_key"] = KMS_KEY
    assert world.run(*ARGV, "--resume") == 0
    assert world.applies == [STACK]  # only the second apply
    assert any("[3/8] skipped (the stack already exports gateway_kms_key)" == x for x in world.out)
    assert any(c[:3] == ("gcloud", "kms", "encrypt") for c in world.shell_calls)


def test_a_resume_that_is_missing_the_stack_file_writes_it(world: World, tmp_path: Path) -> None:
    world.stacks = [STACK]
    assert world.run(*ARGV, "--resume") == 0
    assert (tmp_path / f"Pulumi.{STACK}.yaml").read_text() == (
        f"secretsprovider: {n.SECRETS_PROVIDER}\n"
    )


def test_the_event_log_gives_resources_started_finished_and_failed() -> None:
    lines = [
        json.dumps({"sequence": 1, "resourcePreEvent": {"metadata": {"urn": "a"}}}),
        json.dumps({"sequence": 2, "resourcePreEvent": {"metadata": {"urn": "b"}}}),
        json.dumps({"sequence": 3, "resourcePreEvent": {"metadata": {"urn": "c"}}}),
        json.dumps({"sequence": 4, "resOutputsEvent": {"metadata": {"urn": "a"}}}),
        json.dumps({"sequence": 5, "diagnosticEvent": {"message": "hello"}}),
        json.dumps({"sequence": 6, "resOpFailedEvent": {"metadata": {"urn": "b"}}}),
        '{"sequence": 7, "resourcePreEv',  # still being written
        "",
        "[1]",
    ]
    counts = onboard.count_events(lines)
    assert counts == Counts(started=3, finished=1, failed=1)
    assert counts.in_flight == 1


def test_times_are_minutes_and_seconds() -> None:
    assert onboard.mmss(0) == "00:00"
    assert onboard.mmss(59.6) == "01:00"
    assert onboard.mmss(15 * 60) == "15:00"
    assert onboard.mmss(78 * 60 + 5) == "78:05"


def test_the_budget_is_15_minutes_inclusive() -> None:
    assert onboard.verdict(900, "06:00", partial=False) == (
        "onboarding 15:00 to floor probes (budget 15:00, PASS); certificate 06:00"
    )
    assert "OVER" in onboard.verdict(901, "06:00", partial=False)


def test_the_options_derive_the_cell_s_names() -> None:
    opts = Options(label=LABEL, settings=SETTINGS, probe_image=PROBE_IMAGE)
    assert opts.stack == STACK
    assert opts.project == PROJECT
    assert opts.probe_digest == PROBE_DIGEST
    assert opts.cell_repo == f"{n.REGION}-docker.pkg.dev/{PROJECT}/ssc-apps/apps"


def test_a_real_apply_beats_while_pulumi_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat reads the event log Pulumi writes, here a stand-in script."""
    script = tmp_path / "pulumi"
    script.write_text(
        "#!/bin/sh\n"
        'while [ "$1" != "--event-log" ]; do shift; done\n'
        'echo \'{"resourcePreEvent": {}}\' > "$2"\n'
        'echo \'{"resourcePreEvent": {}}\' >> "$2"\n'
        'echo \'{"resOutputsEvent": {}}\' >> "$2"\n'
        "sleep 0.4\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    beats: list[Counts] = []
    onboard.RealApply(every=0.0, sleep=lambda _s: __import__("time").sleep(0.1))(
        STACK, beats.append
    )
    assert beats
    assert beats[-1] == Counts(started=2, finished=1)


def test_a_real_apply_that_fails_says_where_the_output_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "pulumi"
    script.write_text("#!/bin/sh\necho 'error: quota exceeded'\nexit 3\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    with pytest.raises(CommandError, match=r"(?s)full output in .*up\.log.*quota exceeded"):
        onboard.RealApply(sleep=lambda _s: None)(STACK, lambda _c: None)


def test_the_orchestrator_is_the_one_that_main_builds(world: World) -> None:
    opts = Options(label=LABEL, settings=SETTINGS, probe_image=PROBE_IMAGE)
    assert [s.__name__ for s in Onboarding(opts, world.tools()).steps()] == [
        "stack_and_settings",
        "seed_dns",
        "first_apply",
        "keyring_and_settings",
        "probe_image",
        "second_apply",
        "certificate",
        "probes",
    ]
