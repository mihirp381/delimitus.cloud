"""Onboards a staging cell from one command and times each step (SSC-091):

    uv run python -m ssc_infra.onboard <cell label> --settings settings.json \\
        --probe-image us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform/<probe>@sha256:<digest>

It does what ``infra/README.md`` and the proof run's T1 and T3 do by hand, in order, and prints
each step with the time it took and the total, so the operator sees where the minutes go. The
settings file is a JSON object of stack settings (``gateway_image`` and ``org_id`` are required;
the sealed keyring, its JWKS and ``probe_digest`` are produced here, never given). Settings the
first apply cannot take yet (the gateway's four, and the images that need the org) are set in
step 4.

Each step is idempotent. ``--resume`` continues a stack that exists, skipping a step whose work
is visibly done; ``--from-step N`` starts at step N (and implies ``--resume``). A failed step
prints its time and the command to resume. ``--dry-run`` prints the plan with the exact commands
and runs none. The labels of the live proof run's cells are always refused.

The certificate is Google's, issued only after the first apply creates its DNS record, and the
cell agent is reachable only through the cell's load balancer with that certificate: the floor
probes cannot run before it is ACTIVE. So step 7 waits for it, and its wait is not counted in the
15-minute budget (decision D6); the verdict line shows the counted time and, beside it, how long
the certificate took after the first apply.
"""

import argparse
import base64
import json
import os
import re
import shlex
import subprocess  # noqa: S404
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, cast

from ssc_infra import naming as n
from ssc_infra.cell import APP_IMAGE, CUSTOMER_ORG
from ssc_infra.control import PINNED_IMAGE
from ssc_infra.run import FORBIDDEN_PROJECT, CommandError, pulumi, pulumi_env
from ssc_infra.stack_config import INFRA_DIR, Pulumi
from ssc_shared.hosts import check_cell_label

BUDGET_SECONDS: Final = 15 * 60
CERTIFICATE_TIMEOUT_SECONDS: Final = 120 * 60
CERTIFICATE_POLL_SECONDS: Final = 30.0
HEARTBEAT_SECONDS: Final = 30.0
CERTIFICATE_NAME: Final = "ssc-cell-wildcard"
PROBE_TAG: Final = "nightly-probe"
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
REFUSED_LABELS: Final = frozenset({"proofcell01", "proofcell02"})
"""The live proof run's cells. Nothing here may touch them."""

STEP_NAMES: Final = (
    "stack and settings",
    "DNS sinkhole rules",
    "pulumi up: project, network, registry, gateway key",
    "gateway keyring and settings",
    "probe image into the cell's registry",
    "pulumi up: gateway, agent, probe-runner",
    "certificate issuance (not counted in the budget)",
    "floor probes",
)
SEED_STEP: Final = 2
FIRST_APPLY_STEP: Final = 3
CERTIFICATE_STEP: Final = 7
PROBES_STEP: Final = 8

SETTINGS: Final = frozenset(
    {
        "agent_image",
        "billing_account",
        "build_tools_image",
        "build_frontend_image",
        "gateway_image",
        "gateway_min",
        "gateway_max",
        "org_id",
        "timer_jwks",
        "datagw_image",
        "datagw_connections",
        "proxy_image",
        "oncall_email",
        "warm",
        "database",
        "egress",
        "connections",
        "proxy_ha",
    }
)
"""What ``--settings`` may name. ``stage`` and ``probe`` are set here, and the keyring, its JWKS and
``probe_digest`` are produced here."""
LATE_SETTINGS: Final = (
    "gateway_image",
    "org_id",
    "datagw_image",
    "datagw_connections",
    "proxy_image",
)
"""Left out of the first apply, which refuses the gateway's settings unless all four are there and
the images that need the org unless it is: they arrive in step 4 with the keyring."""
PROBE_IMAGE: Final = re.compile(
    r"[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9._/-]+@(sha256:[0-9a-f]{64})"
)


class OnboardError(Exception):
    pass


class Skip(Exception):  # noqa: N818  (control flow, not an error)
    """A step whose work is already done."""


@dataclass(frozen=True, slots=True)
class Done:
    code: int
    out: bytes
    err: str


class Shell(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,  # noqa: A002
    ) -> Done: ...


def shell(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,  # noqa: A002
) -> Done:
    if any(FORBIDDEN_PROJECT in a for a in argv):
        raise CommandError(f"refusing to touch {FORBIDDEN_PROJECT}")
    result = subprocess.run(  # noqa: S603
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        input=input,
        capture_output=True,
        check=False,
    )
    return Done(result.returncode, result.stdout, result.stderr.decode(errors="replace"))


@dataclass(frozen=True, slots=True)
class Counts:
    started: int = 0
    finished: int = 0
    failed: int = 0

    @property
    def in_flight(self) -> int:
        return max(self.started - self.finished - self.failed, 0)


def count_events(lines: Iterable[str]) -> Counts:
    """Resources started, finished and failed in a ``pulumi up --event-log`` file. A line cut off
    where Pulumi is still writing is skipped."""
    started = finished = failed = 0
    for line in lines:
        try:
            event: Any = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if "resourcePreEvent" in event:
            started += 1
        elif "resOutputsEvent" in event or "resourceOutputsEvent" in event:
            finished += 1
        elif "resOpFailedEvent" in event:
            failed += 1
    return Counts(started, finished, failed)


class Apply(Protocol):
    def __call__(self, stack: str, beat: Callable[[Counts], None]) -> None:
        """Runs ``pulumi up`` on ``stack``, calling ``beat`` with the counts while it runs."""
        ...


def up_args(stack: str, event_log: str) -> tuple[str, ...]:
    """A new stack has nothing to preview against, and the stack was made a moment ago."""
    return (
        "up",
        "--stack",
        stack,
        "--yes",
        "--skip-preview",
        "--non-interactive",
        "--event-log",
        event_log,
    )


class RealApply:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        every: float = HEARTBEAT_SECONDS,
    ) -> None:
        self.clock, self.sleep, self.every = clock, sleep, every

    def __call__(self, stack: str, beat: Callable[[Counts], None]) -> None:
        folder = Path(tempfile.mkdtemp(prefix="ssc-onboard-"))
        events, log = folder / "events.json", folder / "up.log"
        argv = ["pulumi", *up_args(stack, str(events))]
        if any(FORBIDDEN_PROJECT in a for a in argv):
            raise CommandError(f"refusing to touch {FORBIDDEN_PROJECT}")
        with log.open("wb") as out:
            proc = subprocess.Popen(  # noqa: S603
                argv,
                cwd=INFRA_DIR,
                env=dict(pulumi_env()),
                stdout=out,
                stderr=subprocess.STDOUT,
            )
            last = self.clock()
            while proc.poll() is None:
                self.sleep(1.0)
                if self.clock() - last >= self.every:
                    last = self.clock()
                    beat(self._counts(events))
        if proc.returncode:
            tail = log.read_text(errors="replace").strip()[-2000:]
            raise CommandError(f"pulumi up failed (full output in {log}):\n{tail}")

    @staticmethod
    def _counts(events: Path) -> Counts:
        if not events.exists():
            return Counts()
        return count_events(events.read_text(errors="replace").splitlines())


def mmss(seconds: float) -> str:
    whole = int(seconds + 0.5)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def verdict(counted: float, certificate: str, *, partial: bool) -> str:
    """The line the 15 minutes are judged by."""
    word = "PASS" if counted <= BUDGET_SECONDS else "OVER"
    line = (
        f"onboarding {mmss(counted)} to floor probes "
        f"(budget {mmss(BUDGET_SECONDS)}, {word}); certificate {certificate}"
    )
    return line + " [resumed: this run only]" if partial else line


@dataclass(frozen=True, slots=True)
class Options:
    label: str
    settings: Mapping[str, str]
    probe_image: str
    from_step: int = 1
    resume: bool = False
    dry_run: bool = False
    invocation: tuple[str, ...] = ()
    """The arguments to repeat when resuming, without ``--from-step``, ``--resume`` and
    ``--dry-run``."""

    @property
    def resumed(self) -> bool:
        return self.resume or self.from_step > 1

    @property
    def stack(self) -> str:
        return n.cell_stack(self.label)

    @property
    def project(self) -> str:
        return n.cell_project(self.label)

    @property
    def probe_digest(self) -> str:
        match = PROBE_IMAGE.fullmatch(self.probe_image)
        assert match is not None  # checked by validate
        return match.group(1)

    @property
    def cell_repo(self) -> str:
        return f"{n.REGION}-docker.pkg.dev/{self.project}/ssc-apps/{APP_IMAGE}"


def validate(opts: Options) -> None:
    """Everything that can be refused before any command runs."""
    check_cell_label(opts.label)
    if opts.label in REFUSED_LABELS:
        raise ValueError(f"{opts.label} is a live proof-run cell: refusing")
    unknown = sorted(set(opts.settings) - SETTINGS)
    if unknown:
        raise ValueError(
            f"settings {', '.join(unknown)} are not taken here; stage and probe are set, and "
            "gateway_keyring, gateway_jwks and probe_digest are produced"
        )
    missing = [key for key in ("gateway_image", "org_id") if not opts.settings.get(key)]
    if missing:
        raise ValueError(f"settings must include {', '.join(missing)}")
    if not PINNED_IMAGE.fullmatch(opts.settings["gateway_image"]):
        raise ValueError(f"gateway_image must be {n.platform_registry()}/<image>@sha256:<digest>")
    if not CUSTOMER_ORG.fullmatch(opts.settings["org_id"]):
        raise ValueError("org_id must be org_ followed by 20 lowercase letters or digits")
    if PROBE_IMAGE.fullmatch(opts.probe_image) is None:
        raise ValueError("--probe-image must be <registry>/<repository>/<image>@sha256:<digest>")
    if not 1 <= opts.from_step <= len(STEP_NAMES):
        raise ValueError(f"--from-step is 1 to {len(STEP_NAMES)}")


def stack_names(listing: str) -> set[str]:
    """The names in ``pulumi stack ls --json``."""
    try:
        found: Any = json.loads(listing)
    except ValueError:
        return set()
    if not isinstance(found, list):
        return set()
    items = cast("list[object]", found)
    names: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            names.add(str(cast("dict[str, object]", item).get("name")))
    return names


def pulumi_line(args: Sequence[str]) -> str:
    return "pulumi " + shlex.join([*args, "--non-interactive"])


def early_settings(opts: Options) -> dict[str, str]:
    held = set(LATE_SETTINGS)
    return {
        "gcp:disableGlobalProjectWarning": "true",
        "stage": "staging",
        "probe": "true",
        **{k: v for k, v in opts.settings.items() if k not in held},
    }


def late_settings(opts: Options, keyring: str, jwks: str) -> dict[str, str]:
    return {
        **{k: opts.settings[k] for k in LATE_SETTINGS if k in opts.settings},
        "gateway_keyring": keyring,
        "gateway_jwks": jwks,
    }


def set_all_args(stack: str, settings: Mapping[str, str]) -> tuple[str, ...]:
    plain = [arg for k, v in settings.items() for arg in ("--plaintext", f"{k}={v}")]
    return ("config", "set-all", "--stack", stack, *plain)


def keys_new() -> list[str]:
    return ["uv", "run", "python", "-m", "ssc_edge.keys", "new"]


def keys_jwks() -> list[str]:
    return ["uv", "run", "python", "-m", "ssc_edge.keys", "jwks"]


def seal(key: str) -> list[str]:
    """The keyring comes in on stdin and the ciphertext goes out on stdout: the plain keyring is
    never written to disk."""
    return [
        "gcloud",
        "kms",
        "encrypt",
        f"--key={key}",
        "--plaintext-file=-",
        "--ciphertext-file=-",
        "--quiet",
    ]


def image_exists(project: str, reference: str) -> list[str]:
    return [
        "gcloud",
        "artifacts",
        "docker",
        "images",
        "describe",
        reference,
        f"--project={project}",
        "--quiet",
    ]


def copy_image(opts: Options) -> list[str]:
    """Keeps the digest: the same manifest, in the cell's own repository (as T3 does by hand)."""
    return [
        "docker",
        "buildx",
        "imagetools",
        "create",
        "--tag",
        f"{opts.cell_repo}:{PROBE_TAG}",
        opts.probe_image,
    ]


def certificate_state(project: str) -> list[str]:
    return [
        "gcloud",
        "certificate-manager",
        "certificates",
        "describe",
        CERTIFICATE_NAME,
        f"--project={project}",
        "--location=global",
        "--format=value(managed.state)",
        "--quiet",
    ]


def floor_probes() -> list[str]:
    return ["uv", "run", "python", "-m", "ssc_conformance.nightly"]


def probe_env(opts: Options, base: Mapping[str, str]) -> dict[str, str]:
    return {
        **base,
        "SSC_PROBE_PROJECT": opts.project,
        "SSC_PROBE_AGENT_URL": n.agent_url(opts.label),
        "SSC_PROBE_DIGEST": opts.probe_digest,
    }


@dataclass(slots=True)
class Tools:
    pulumi: Pulumi = pulumi
    shell: Shell = shell
    apply: Apply = field(default_factory=RealApply)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    out: Callable[[str], None] = lambda text: print(text, flush=True)  # noqa: T201
    err: Callable[[str], None] = lambda text: print(text, file=sys.stderr, flush=True)  # noqa: T201
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    infra_dir: str = INFRA_DIR
    repo_root: Path = REPO_ROOT


class Onboarding:
    def __init__(self, opts: Options, tools: Tools) -> None:
        self.opts = opts
        self.t = tools
        self.started = 0.0
        self.first_apply_done: float | None = None
        self.certificate_ready: float | None = None
        self.certificate_wait = 0.0
        self.certificate_already_active = False
        self.detected_skips = False

    # The steps, in order. Each raises Skip when its work is already done.

    def stack_and_settings(self) -> None:
        opts = self.opts
        stacks = self.t.pulumi("stack", "ls", "--json", cwd=self.t.infra_dir)
        exists = opts.stack in stack_names(stacks)
        if exists and not opts.resumed:
            raise OnboardError(f"stack {opts.stack} already exists: pass --resume to continue it")
        if not exists:
            self.t.pulumi(
                "stack",
                "init",
                opts.stack,
                f"--secrets-provider={n.SECRETS_PROVIDER}",
                cwd=self.t.infra_dir,
            )
        else:
            stack_file = Path(self.t.infra_dir) / f"Pulumi.{opts.stack}.yaml"
            if not stack_file.exists():
                stack_file.write_text(f"secretsprovider: {n.SECRETS_PROVIDER}\n")
        self.t.pulumi(*set_all_args(opts.stack, early_settings(opts)), cwd=self.t.infra_dir)

    def seed_dns(self) -> None:
        raise Skip("skipped (SSC-091 phase 2)")

    def first_apply(self) -> None:
        if self.detected(self._has_output("gateway_kms_key")):
            raise Skip("skipped (the stack already exports gateway_kms_key)")
        self._apply(FIRST_APPLY_STEP)
        self.first_apply_done = self.t.clock()

    def keyring_and_settings(self) -> None:
        opts = self.opts
        if self.detected(self._has_config("gateway_keyring")):
            raise Skip("skipped (gateway_keyring is already set)")
        key = self.t.pulumi(
            "stack", "output", "--stack", opts.stack, "gateway_kms_key", cwd=self.t.infra_dir
        ).strip()
        root = self.t.repo_root
        keyring = self._sh(keys_new(), cwd=root).out
        jwks = self._sh(keys_jwks(), cwd=root, input=keyring).out.decode().strip()
        sealed = base64.b64encode(self._sh(seal(key), input=keyring).out).decode("ascii")
        del keyring
        settings = late_settings(opts, sealed, jwks)
        self.t.pulumi(*set_all_args(opts.stack, settings), cwd=self.t.infra_dir)

    def probe_image(self) -> None:
        opts = self.opts
        reference = f"{opts.cell_repo}@{opts.probe_digest}"
        copied = self.opts.resumed and self.t.shell(image_exists(opts.project, reference)).code == 0
        configured = self.opts.resumed and self._config_value("probe_digest") == opts.probe_digest
        if copied and configured:
            self.detected_skips = True
            raise Skip("skipped (the image is in the registry and probe_digest is set)")
        if not copied:
            self._sh(copy_image(opts))
        self.t.pulumi(
            "config",
            "set",
            "--stack",
            opts.stack,
            "probe_digest",
            opts.probe_digest,
            cwd=self.t.infra_dir,
        )

    def second_apply(self) -> None:
        done = self._config_output()
        if self.detected(done is not None and "gateway_keyring" in done and "probe_digest" in done):
            raise Skip("skipped (the stack was applied with the keyring and the probe digest)")
        self._apply(CERTIFICATE_STEP - 1)

    def certificate(self) -> None:
        """Waits for the certificate; the wait is left out of the 15 minutes."""
        project = self.opts.project
        began = self.t.clock()
        last_beat = began
        polls = 0
        while True:
            state = self._certificate_state(project)
            polls += 1
            if state == "ACTIVE":
                break
            if state == "FAILED":
                raise OnboardError("the certificate FAILED: see Certificate Manager in the cell")
            now = self.t.clock()
            if now - began > CERTIFICATE_TIMEOUT_SECONDS:
                raise OnboardError(
                    f"the certificate is {state or 'unknown'} after "
                    f"{mmss(CERTIFICATE_TIMEOUT_SECONDS)}"
                )
            if now - last_beat >= 2 * CERTIFICATE_POLL_SECONDS:
                last_beat = now
                self.t.out(f"      ... certificate {state or 'unknown'}, {mmss(now - began)}")
            self.t.sleep(CERTIFICATE_POLL_SECONDS)
        if polls == 1 and self.opts.resumed:
            self.certificate_already_active = True
        self.certificate_ready = self.t.clock()
        self.certificate_wait = self.certificate_ready - began

    def probes(self) -> None:
        opts = self.opts
        done = self.t.shell(floor_probes(), cwd=self.t.repo_root, env=probe_env(opts, self.t.env))
        report = done.out.decode(errors="replace").strip()
        if report:
            self.t.out(report)
        if done.code:
            raise OnboardError(
                f"the floor probes did not all pass (exit {done.code})\n{done.err.strip()[-1500:]}"
            )

    # Helpers.

    def detected(self, done: bool) -> bool:  # noqa: FBT001
        """Whether a ``--resume`` finds this step's work done. A fresh run never asks."""
        if self.opts.resumed and done:
            self.detected_skips = True
            return True
        return False

    def _sh(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        input: bytes | None = None,  # noqa: A002
    ) -> Done:
        """Fails with the command and its stderr, never its stdout (a keyring may be on it)."""
        done = self.t.shell(argv, cwd=cwd, env=None, input=input)
        if done.code:
            raise CommandError(
                f"{argv[0]} {' '.join(argv[1:3])} failed (exit {done.code}): "
                f"{done.err.strip()[-1000:]}"
            )
        return done

    def _has_output(self, name: str) -> bool:
        try:
            self.t.pulumi("stack", "output", "--stack", self.opts.stack, name, cwd=self.t.infra_dir)
        except CommandError:
            return False
        return True

    def _config_output(self) -> dict[str, Any] | None:
        try:
            raw = self.t.pulumi(
                "stack",
                "output",
                "--stack",
                self.opts.stack,
                "--json",
                "config",
                cwd=self.t.infra_dir,
            )
            found: Any = json.loads(raw)
        except CommandError, ValueError:
            return None
        return cast("dict[str, Any]", found) if isinstance(found, dict) else None

    def _config_value(self, key: str) -> str | None:
        try:
            return self.t.pulumi(
                "config", "get", "--stack", self.opts.stack, key, cwd=self.t.infra_dir
            ).strip()
        except CommandError:
            return None

    def _has_config(self, key: str) -> bool:
        return bool(self._config_value(key))

    def _certificate_state(self, project: str) -> str:
        done = self.t.shell(certificate_state(project))
        return done.out.decode().strip() if done.code == 0 else ""

    def _apply(self, step: int) -> None:
        began = self.t.clock()

        def beat(counts: Counts) -> None:
            self.t.out(
                f"      ... {counts.finished} resources done, {counts.in_flight} in flight, "
                f"{mmss(self.t.clock() - began)}"
            )

        self.t.apply(self.opts.stack, beat)

    # The run.

    def steps(self) -> list[Callable[[], None]]:
        return [
            self.stack_and_settings,
            self.seed_dns,
            self.first_apply,
            self.keyring_and_settings,
            self.probe_image,
            self.second_apply,
            self.certificate,
            self.probes,
        ]

    def resume_command(self, step: int) -> str:
        return shlex.join(
            ["uv", "run", "python", "-m", "ssc_infra.onboard", *self.opts.invocation]
            + ["--from-step", str(step)]
        )

    def run(self) -> int:
        opts, t, total = self.opts, self.t, len(STEP_NAMES)
        self.started = t.clock()
        t.out(f"onboarding {opts.stack} (project {opts.project}), {total} steps")
        for number, (name, step) in enumerate(zip(STEP_NAMES, self.steps(), strict=True), 1):
            tag = f"[{number}/{total}]"
            if number < opts.from_step:
                t.out(f"{tag} {name}: skipped (--from-step {opts.from_step})")
                self.detected_skips = True
                continue
            t.out(f"{tag} {name}")
            began = t.clock()
            try:
                step()
            except Skip as reason:
                t.out(f"{tag} {reason}")
                continue
            except (CommandError, OnboardError, ValueError) as exc:
                spent, so_far = mmss(t.clock() - began), mmss(t.clock() - self.started)
                t.out(f"{tag} FAILED after {spent}, total {so_far}")
                t.err(str(exc))
                t.err(f"resume with: {self.resume_command(number)}")
                return 1
            now = t.clock()
            t.out(f"{tag} done in {mmss(now - began)}, total {mmss(now - self.started)}")
        t.out(self.verdict_line())
        return 0

    def certificate_figure(self) -> str:
        if self.certificate_already_active:
            return "already active"
        if self.certificate_ready is None:
            return "not measured (step skipped)"
        if self.first_apply_done is None:
            return "not measured (resumed after the first apply)"
        return mmss(self.certificate_ready - self.first_apply_done)

    def verdict_line(self) -> str:
        counted = self.t.clock() - self.started - self.certificate_wait
        return verdict(
            counted,
            self.certificate_figure(),
            partial=self.opts.from_step > 1 or self.detected_skips,
        )


def plan(opts: Options) -> list[str]:
    """What each step would run, from the same builders the steps use. Values only known while
    running appear in angle brackets."""
    early = early_settings(opts)
    late = late_settings(opts, "<sealed keyring>", "<identity JWKS>")
    log = "<event log in a temporary folder>"
    reference = f"{opts.cell_repo}@{opts.probe_digest}"
    steps: list[list[str]] = [
        [
            pulumi_line(("stack", "ls", "--json")),
            pulumi_line(("stack", "init", opts.stack, f"--secrets-provider={n.SECRETS_PROVIDER}"))
            + "  (when the stack is new)",
            pulumi_line(set_all_args(opts.stack, early)),
        ],
        ["nothing yet: skipped (SSC-091 phase 2)"],
        ["pulumi " + shlex.join(up_args(opts.stack, log))],
        [
            pulumi_line(("stack", "output", "--stack", opts.stack, "gateway_kms_key")),
            shlex.join(keys_new()) + "  (in " + str(REPO_ROOT) + ")",
            shlex.join(keys_jwks()) + "  (keyring on stdin)",
            shlex.join(seal("<gateway_kms_key>")) + "  (keyring on stdin)",
            pulumi_line(set_all_args(opts.stack, late)),
        ],
        [
            shlex.join(image_exists(opts.project, reference)) + "  (with --resume)",
            shlex.join(copy_image(opts)),
            pulumi_line(
                ("config", "set", "--stack", opts.stack, "probe_digest", opts.probe_digest)
            ),
        ],
        ["pulumi " + shlex.join(up_args(opts.stack, log))],
        [
            shlex.join(certificate_state(opts.project))
            + f"  (every {CERTIFICATE_POLL_SECONDS:.0f} s, "
            + f"up to {mmss(CERTIFICATE_TIMEOUT_SECONDS)})"
        ],
        [
            f"SSC_PROBE_PROJECT={opts.project} SSC_PROBE_AGENT_URL={n.agent_url(opts.label)} "
            f"SSC_PROBE_DIGEST={opts.probe_digest} "
            + shlex.join(floor_probes())
            + f"  (in {REPO_ROOT})"
        ],
    ]
    lines = [f"plan for {opts.stack} (project {opts.project}); nothing is run"]
    for number, (name, commands) in enumerate(zip(STEP_NAMES, steps, strict=True), 1):
        lines.append(f"[{number}/{len(STEP_NAMES)}] {name}")
        lines += [f"      $ {c}" for c in commands]
    lines.append(f"counted: steps 1 to 6 and 8; the budget is {mmss(BUDGET_SECONDS)}")
    return lines


def parse(argv: Sequence[str]) -> Options:
    parser = argparse.ArgumentParser(
        prog="python -m ssc_infra.onboard",
        description=(__doc__ or "").split("\n\n", maxsplit=1)[0],
    )
    parser.add_argument("label")
    parser.add_argument("--settings", type=Path, help="JSON object of stack settings")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--probe-image", required=True)
    parser.add_argument("--from-step", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    settings: dict[str, str] = {}
    if args.settings is not None:
        loaded: Any = json.loads(args.settings.read_text())
        if not isinstance(loaded, dict):
            raise ValueError("--settings must hold a JSON object")
        settings |= {str(k): str(v) for k, v in loaded.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"--set wants KEY=VALUE, not {item!r}")
        settings[key] = value
    repeated = [args.label, "--probe-image", args.probe_image]
    if args.settings is not None:
        repeated += ["--settings", str(args.settings)]
    for item in args.set:
        repeated += ["--set", item]
    return Options(
        label=args.label,
        settings=settings,
        probe_image=args.probe_image,
        from_step=args.from_step,
        resume=args.resume,
        dry_run=args.dry_run,
        invocation=tuple(repeated),
    )


def main(argv: Sequence[str], tools: Tools | None = None) -> int:
    tools = tools or Tools()
    try:
        opts = parse(argv)
        validate(opts)
    except (ValueError, OSError) as exc:
        tools.err(str(exc))
        return 2
    if opts.dry_run:
        for line in plan(opts):
            tools.out(line)
        return 0
    return Onboarding(opts, tools).run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
