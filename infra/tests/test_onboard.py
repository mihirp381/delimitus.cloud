"""``ssc_infra.onboard`` runs the steps in order, times them, and says where to resume. Nothing
here runs a real command: Pulumi, the shell, the clock and ``pulumi up`` are fakes."""

import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
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
SEED_SKIP = "not needed: the rules go in the first apply on local state (SSC-091)"
URNS = [
    f"urn:pulumi:{STACK}::ssc-infra::{kind}::{name}"
    for kind, name in (
        ("pulumi:pulumi:Stack", "ssc-infra-" + STACK),
        ("gcp:compute/network:Network", "net"),
        ("gcp:dns/recordSet:RecordSet", "rule-1"),
    )
]
BUCKET = f"gs://{n.STATE_BUCKET}"
KEY_LINES = "secretsprovider: gcpkms://k\nencryptedkey: SEALED-DATA-KEY\n"


class World:
    """The fake machine, with a clock that only the fakes move."""

    def __init__(self, tmp_path: Path) -> None:
        self.now = 1000.0
        self.pulumi_calls: list[tuple[str, ...]] = []
        self.shell_calls: list[tuple[str, ...]] = []
        self.shell_envs: list[Mapping[str, str] | None] = []
        self.applies: list[str] = []
        self.stacks: list[str] = []
        """Stacks in the local file backend; ``bucket`` is the bucket's, by resource count."""
        self.bucket: dict[str, int] = {}
        self.local_urns = list(URNS)
        self.bucket_urns: dict[str, list[str]] = {}
        self.pulumi_envs: list[Mapping[str, str] | None] = []
        self.preview_seconds = 0.0
        self.apply_envs: list[Mapping[str, str]] = []
        self.init_rewrites_yaml = False
        self.state_root = tmp_path / "state"
        self.folder = self.state_root / LABEL
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

    def stack_file(self) -> Path:
        return self.tmp_path / f"Pulumi.{STACK}.yaml"

    def pulumi(
        self, *args: str, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> str:
        self.pulumi_calls.append(args)
        self.pulumi_envs.append(env)
        assert env is not None
        url = env["PULUMI_BACKEND_URL"]
        in_bucket = url == BUCKET
        assert in_bucket or url == f"file://{self.folder}"
        if args[0] == "preview":
            self.now += self.preview_seconds
        if self.fail(args):
            raise CommandError("pulumi " + " ".join(args[:2]) + " failed: boom")
        if args[:2] == ("stack", "ls"):
            if in_bucket:
                return json.dumps(
                    [
                        {"name": s, **({"resourceCount": c} if c else {})}
                        for s, c in self.bucket.items()
                    ]
                )
            return json.dumps([{"name": s} for s in self.stacks])
        if args[:2] == ("stack", "init"):
            if in_bucket:
                self.bucket[args[2]] = 0
                if self.init_rewrites_yaml:
                    self.stack_file().write_text("secretsprovider: gcpkms://k\nencryptedkey: NEW\n")
            else:
                self.stacks.append(args[2])
                self.stack_file().write_text(KEY_LINES)
        if args[:2] == ("stack", "export"):
            urns = self.bucket_urns.get(STACK, []) if in_bucket else self.local_urns
            deployment = {"resources": [{"urn": u, "type": "x"} for u in urns]}
            Path(args[-1]).write_text(json.dumps({"version": 3, "deployment": deployment}))
        if args[:2] == ("stack", "import"):
            assert in_bucket
            self.bucket_urns[STACK] = list(self.local_urns)
            self.bucket[STACK] = len(self.local_urns)
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

    def apply(self, stack: str, beat: Callable[[Counts], None], env: Mapping[str, str]) -> None:
        self.applies.append(stack)
        self.apply_envs.append(env)
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
            state_root=self.state_root,
            now=lambda: datetime(2026, 10, 6, 12, 30, 5, tzinfo=UTC),
        )

    def run(self, *argv: str) -> int:
        settings = self.tmp_path / "settings.json"
        settings.write_text(json.dumps(SETTINGS))
        return onboard.main([*argv, "--settings", str(settings)], self.tools())

    def commands(self) -> list[str]:
        return [" ".join(a) for a in self.shell_calls]

    def pulumi_lines(self) -> list[str]:
        return [" ".join(a) for a in self.pulumi_calls]

    def local_calls(self) -> list[tuple[str, ...]]:
        return [
            c
            for c, e in zip(self.pulumi_calls, self.pulumi_envs, strict=True)
            if e is not None and e["PULUMI_BACKEND_URL"].startswith("file://")
        ]

    def bucket_calls(self) -> list[tuple[str, ...]]:
        return [
            c
            for c, e in zip(self.pulumi_calls, self.pulumi_envs, strict=True)
            if e is not None and e["PULUMI_BACKEND_URL"] == BUCKET
        ]

    def moved(self) -> list[Path]:
        return sorted(self.state_root.glob(f"{LABEL}.moved-*"))


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
    assert f"[2/9] {SEED_SKIP}" in world.out
    assert any(line.startswith("[3/9] done in 01:00, total ") for line in world.out)
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
    assert "[7/9] FAILED after 00:32, total " in "\n".join(world.out)
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
    assert any(line.startswith("[5/9] FAILED after 00:42, total ") for line in world.out)
    assert "docker buildx imagetools failed (exit 1): docker: boom" in world.err[0]
    assert world.err[1] == (
        f"resume with: uv run python -m ssc_infra.onboard {LABEL} --probe-image {PROBE_IMAGE} "
        f"--settings {tmp_path / 'settings.json'} --from-step 5"
    )
    assert world.out[-1].startswith("[5/9] FAILED")
    assert len(world.applies) == 1


def test_a_failed_pulumi_up_names_step_3(world: World) -> None:
    world.fail = lambda argv: argv == ("apply", STACK)
    assert world.run(*ARGV) == 1
    assert any(line.startswith("[3/9] FAILED") for line in world.out)
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
        ([*ARGV, "--from-step", "10"], "--from-step is 1 to 9"),
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
        assert f"[{number}/9] {name}" in text
    assert f"$ pulumi stack init {STACK} --secrets-provider={n.SECRETS_PROVIDER}" in text
    assert f"$ pulumi up --stack {STACK} --yes --skip-preview --non-interactive --event-log" in text
    assert f"docker buildx imagetools create --tag {n.REGION}-docker.pkg.dev/{PROJECT}" in text
    assert PROBE_IMAGE in text
    assert f"--project={PROJECT} --location=global" in text
    assert f"SSC_PROBE_AGENT_URL={n.agent_url(LABEL)}" in text
    assert SEED_SKIP not in text
    assert "gateway_keyring=<sealed keyring>" in text
    assert "gateway_image=" not in text.split("[4/9]")[0]


def test_from_step_skips_the_earlier_steps_and_marks_the_verdict_as_this_run_only(
    world: World,
) -> None:
    world.stacks = [STACK]
    world.outputs["gateway_kms_key"] = KMS_KEY
    world.config["gateway_keyring"] = "set"
    world.config["probe_digest"] = PROBE_DIGEST
    world.image_in_registry = True
    world.stack_file().write_text(KEY_LINES)  # step 1 is skipped, so nothing writes it
    assert world.run(*ARGV, "--from-step", "6") == 0
    assert "[1/9] stack and settings: skipped (--from-step 6)" in world.out
    assert not any(c[:2] == ("stack", "init") for c in world.local_calls())
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
    assert len(skipped) == 4
    assert f"[2/9] {SEED_SKIP}" in world.out
    assert not any("imagetools" in c for c in world.commands())
    assert world.out[-1].endswith("certificate already active [resumed: this run only]")


def test_resume_picks_up_where_the_first_apply_finished(world: World) -> None:
    world.stacks = [STACK]
    world.outputs["gateway_kms_key"] = KMS_KEY
    assert world.run(*ARGV, "--resume") == 0
    assert world.applies == [STACK]  # only the second apply
    assert any("[3/9] skipped (the stack already exports gateway_kms_key)" == x for x in world.out)
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
        f'echo "$PULUMI_BACKEND_URL" > "{tmp_path}/backend"\n'
        "sleep 0.4\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    beats: list[Counts] = []
    env = {**os.environ, "PULUMI_BACKEND_URL": "file:///kept/state"}
    onboard.RealApply(every=0.0, sleep=lambda _s: __import__("time").sleep(0.1))(
        STACK, beats.append, env
    )
    assert (tmp_path / "backend").read_text().strip() == "file:///kept/state"
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
        onboard.RealApply(sleep=lambda _s: None)(STACK, lambda _c: None, dict(os.environ))


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
        "move_state",
    ]


# SSC-091 phase 2b: the state is local until step 9 moves it to the bucket.


def have_local_state(world: World, *, yaml: bool = True) -> None:
    world.folder.mkdir(parents=True)
    world.stacks = [STACK]
    if yaml:
        world.stack_file().write_text(KEY_LINES)


def test_every_pulumi_call_before_the_move_runs_on_the_local_folder_and_the_bucket_is_only_asked(
    world: World,
) -> None:
    assert world.run(*ARGV) == 0
    assert world.pulumi_calls
    for call, env in zip(world.pulumi_calls, world.pulumi_envs, strict=True):
        assert env is not None
        assert env["PULUMI_BACKEND_URL"] in {f"file://{world.folder}", BUCKET}, call
        assert env["HOME"] == "/home/x"  # the whole environment goes along, not just the backend
    assert [c[:2] for c in world.bucket_calls()] == [
        ("stack", "ls"),  # the preflight
        ("stack", "ls"),  # the move
        ("stack", "init"),
        ("stack", "import"),
        ("stack", "export"),
        ("preview", "--stack"),
    ]
    assert world.applies == [STACK, STACK]
    for env in world.apply_envs:
        assert env["PULUMI_BACKEND_URL"] == f"file://{world.folder}"


def test_the_move_copies_the_state_checks_it_and_keeps_the_local_folder_renamed(
    world: World,
) -> None:
    assert world.run(*ARGV) == 0
    bucket = world.bucket_calls()
    assert bucket[2] == (
        "stack",
        "init",
        STACK,
        f"--secrets-provider={n.SECRETS_PROVIDER}",
    )
    assert bucket[-1] == (
        "preview",
        "--stack",
        STACK,
        "--expect-no-changes",
        "--parallel",
        "32",
    )
    assert not world.folder.exists()
    (moved,) = world.moved()
    assert moved.name == f"{LABEL}.moved-20261006T123005Z"
    for kept in ("export.json", f"Pulumi.{STACK}.yaml.before-init", "bucket-export.json"):
        assert (moved / kept).exists()
    assert (moved / f"Pulumi.{STACK}.yaml.before-init").read_text() == KEY_LINES
    assert stat.S_IMODE(moved.stat().st_mode) == 0o700
    assert stat.S_IMODE((moved / "export.json").stat().st_mode) == 0o600
    assert world.out[-3].startswith("[9/9] done in ")
    assert world.out[-2].startswith("state move: ")
    assert world.out[-1].startswith("onboarding ")
    assert f"the local copy is kept at {moved}" in "\n".join(world.out)
    assert "SEALED-DATA-KEY" not in "\n".join(world.out + world.err + world.pulumi_lines())


def test_the_move_is_counted_in_the_total_and_the_budget_and_has_its_own_line(
    world: World,
) -> None:
    world.preview_seconds = 14 * 60  # a slow preview of a 7 MB state
    assert world.run(*ARGV) == 0
    assert world.out[-2] == "state move: 14:00 (counted in the total)"
    assert "(budget 15:00, OVER)" in world.out[-1]


def test_a_quick_move_leaves_the_run_within_the_budget(world: World) -> None:
    assert world.run(*ARGV) == 0
    assert world.out[-2] == "state move: 00:00 (counted in the total)"
    assert "(budget 15:00, PASS)" in world.out[-1]


def test_the_state_folder_is_kept_in_the_home_folder_not_a_temporary_one() -> None:
    assert onboard.STATE_ROOT == Path.home() / ".ssc" / "onboard"
    assert Tools().state_root == onboard.STATE_ROOT
    temporary = Path(tempfile.gettempdir()).resolve()
    assert not onboard.STATE_ROOT.resolve().is_relative_to(temporary)


def test_the_state_folder_is_private_whatever_the_umask(world: World) -> None:
    previous = os.umask(0o000)
    try:
        world.fail = lambda argv: argv[:2] == ("stack", "export")  # stops in step 9
        assert world.run(*ARGV) == 1
    finally:
        os.umask(previous)
    assert stat.S_IMODE(world.folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(world.state_root.stat().st_mode) == 0o700


def test_both_applies_take_parallel_32() -> None:
    args = onboard.up_args(STACK, "/x/events.json")
    assert args[-2:] == ("--parallel", "32")
    assert args[:3] == ("up", "--stack", STACK)


def test_the_dry_run_shows_the_folder_the_backend_of_each_step_and_the_move(world: World) -> None:
    assert world.run(*ARGV, "--dry-run") == 0
    assert world.pulumi_calls == []
    text = "\n".join(world.out)
    folder = onboard.STATE_ROOT / LABEL
    assert f"state: {folder} (mode 0700)" in text
    assert text.count(f"backend: file://{folder}") == 8
    assert "[2/9] DNS sinkhole rules" in text
    assert "nothing: the rules go in the first apply on local state (SSC-091)" in text
    assert "--parallel 32" in text.split("[3/9]")[1].split("[4/9]")[0]
    assert "--parallel 32" in text.split("[6/9]")[1].split("[7/9]")[0]
    move = text.split("[9/9] move the state to the bucket")[1]
    assert f"$ pulumi stack export --stack {STACK} --file {folder}/export.json" in move
    assert f"cp infra/Pulumi.{STACK}.yaml {folder}/Pulumi.{STACK}.yaml.before-init" in move
    assert f"$ pulumi stack init {STACK} --secrets-provider={n.SECRETS_PROVIDER}" in move
    assert f"[{BUCKET}]" in move
    assert f"$ pulumi stack import --stack {STACK} --file {folder}/export.json" in move
    assert f"$ pulumi preview --stack {STACK} --expect-no-changes --parallel 32" in move
    assert f"mv {folder} {folder}.moved-<UTC>" in move
    assert "counted: steps 1 to 6, 8 and 9" in text


def test_a_stack_that_exists_only_in_the_bucket_is_refused_before_step_1(world: World) -> None:
    world.bucket = {STACK: 120}
    assert world.run(*ARGV) == 1
    assert f"stack {STACK} already exists in {BUCKET}" in world.err[0]
    assert "onboarded before SSC-091 phase 2b lives only in the bucket" in world.err[0]
    assert "this command does not resume it" in world.err[0]
    assert world.local_calls() == []
    assert world.applies == []
    assert not world.state_root.exists()


def test_a_resume_does_not_take_over_a_bucket_stack_either(world: World) -> None:
    world.bucket = {STACK: 120}
    assert world.run(*ARGV, "--resume") == 1
    assert "this command does not resume it" in world.err[0]
    assert world.applies == []


def test_local_state_and_a_bucket_stack_together_are_refused_with_the_exact_situation(
    world: World,
) -> None:
    have_local_state(world)
    world.bucket = {STACK: 0}
    assert world.run(*ARGV, "--resume") == 1
    assert (
        f"local state at {world.folder} and the stack {STACK} in {BUCKET} both exist"
        in (world.err[0])
    )
    assert world.err[0].endswith("Otherwise remove one of the two by hand.")
    assert "--from-step 9" in world.err[0]
    assert world.applies == []
    assert world.local_calls() == []


def test_a_stack_whose_state_was_moved_is_already_onboarded(world: World) -> None:
    world.bucket = {STACK: 3}
    moved = world.state_root / f"{LABEL}.moved-20260101T000000Z"
    moved.mkdir(parents=True)
    for argv in (ARGV, [*ARGV, "--resume"], [*ARGV, "--from-step", "9"]):
        world.err.clear()
        assert world.run(*argv) == 1
        assert world.err[0] == (
            f"{STACK} is already onboarded: its state was moved to {BUCKET} and the local copy "
            f"is kept at {moved}"
        )
    assert world.local_calls() == []
    assert world.applies == []
    assert moved.exists()


def test_the_move_alone_needs_local_state_to_move(world: World) -> None:
    assert world.run(*ARGV, "--from-step", "9") == 1
    assert f"there is no local state at {world.folder} to move to {BUCKET}" in world.err[0]
    assert world.bucket_calls() == [("stack", "ls", "--json")]


def test_a_resume_continues_on_the_local_folder_and_ends_with_the_move(world: World) -> None:
    have_local_state(world)
    world.outputs["gateway_kms_key"] = KMS_KEY
    assert world.run(*ARGV, "--resume") == 0
    assert world.applies == [STACK]
    assert ("stack", "init", STACK, f"--secrets-provider={n.SECRETS_PROVIDER}") not in (
        world.local_calls()
    )
    assert len(world.moved()) == 1


def test_a_move_that_stopped_after_the_init_is_finished_by_from_step_9(world: World) -> None:
    world.fail = lambda argv: argv[:2] == ("stack", "import")
    assert world.run(*ARGV) == 1
    assert world.out[-1].startswith("[9/9] FAILED")
    assert world.err[-1].endswith("--from-step 9")
    assert world.bucket == {STACK: 0}
    assert world.folder.exists()
    assert world.moved() == []
    world.fail = lambda _argv: False
    world.err.clear()
    assert world.run(*ARGV, "--from-step", "9") == 0
    inits = [c for c in world.bucket_calls() if c[:2] == ("stack", "init")]
    assert len(inits) == 1  # the retry did not init again
    assert world.bucket == {STACK: len(URNS)}
    assert len(world.moved()) == 1
    assert world.err == []


def test_a_move_that_stopped_after_the_import_goes_straight_to_the_preview(world: World) -> None:
    have_local_state(world)
    world.bucket = {STACK: len(URNS)}
    world.bucket_urns = {STACK: list(URNS)}
    assert world.run(*ARGV, "--from-step", "9") == 0
    kinds = [c[:2] for c in world.bucket_calls()]
    assert ("stack", "init") not in kinds
    assert ("stack", "import") not in kinds
    assert kinds[-1] == ("preview", "--stack")
    assert len(world.moved()) == 1


def test_a_preview_with_changes_leaves_the_local_folder_and_the_retry_runs_it_again(
    world: World,
) -> None:
    world.fail = lambda argv: argv[:1] == ("preview",)
    assert world.run(*ARGV) == 1
    assert world.folder.exists()
    assert world.moved() == []
    world.fail = lambda _argv: False
    assert world.run(*ARGV, "--from-step", "9") == 0
    kinds = [c[:2] for c in world.bucket_calls()]
    assert kinds.count(("stack", "import")) == 1
    assert len(world.moved()) == 1


def test_a_bucket_stack_with_other_state_is_never_overwritten(world: World) -> None:
    have_local_state(world)
    world.bucket = {STACK: 2}
    world.bucket_urns = {STACK: URNS[:2]}
    assert world.run(*ARGV, "--from-step", "9") == 1
    assert "holds 2 resources and the local state 3, and they are not the same" in world.err[0]
    assert "nothing was overwritten" in world.err[0]
    kinds = [c[:2] for c in world.bucket_calls()]
    assert ("stack", "import") not in kinds
    assert not any(k[0] == "preview" for k in kinds)
    assert world.folder.exists()


def test_a_stack_init_that_rewrites_the_key_lines_stops_before_the_import(world: World) -> None:
    have_local_state(world)
    world.init_rewrites_yaml = True
    assert world.run(*ARGV, "--from-step", "9") == 1
    saved = world.folder / f"Pulumi.{STACK}.yaml.before-init"
    assert world.err[0].startswith("encryptedkey changed in ")
    assert f"The saved copy is at {saved}" in world.err[0]
    assert world.err[0].endswith("Nothing was imported and nothing was restored.")
    assert saved.read_text() == KEY_LINES
    assert world.stack_file().read_text() != KEY_LINES  # not restored
    assert ("stack", "import") not in [c[:2] for c in world.bucket_calls()]
    assert world.folder.exists()
    everything = "\n".join(world.out + world.err)
    assert "SEALED-DATA-KEY" not in everything
    assert "NEW" not in everything.replace("changed", "")
    # the retry meets the same guard: the saved copy is the original, and is never overwritten
    world.err.clear()
    assert world.run(*ARGV, "--from-step", "9") == 1
    assert world.err[0].startswith("encryptedkey changed in ")
    assert saved.read_text() == KEY_LINES
    assert ("stack", "import") not in [c[:2] for c in world.bucket_calls()]


def test_the_move_refuses_without_the_stack_file_and_without_resources(world: World) -> None:
    have_local_state(world, yaml=False)
    assert world.run(*ARGV, "--from-step", "9") == 1
    assert "is missing: a stack init without it would make a new key" in world.err[0]
    assert ("stack", "init") not in [c[:2] for c in world.bucket_calls()]
    world.err.clear()
    world.stack_file().write_text(KEY_LINES)
    world.local_urns = []
    assert world.run(*ARGV, "--from-step", "9") == 1
    assert "the local stack holds no resources" in world.err[0]
    assert ("stack", "init") not in [c[:2] for c in world.bucket_calls()]
