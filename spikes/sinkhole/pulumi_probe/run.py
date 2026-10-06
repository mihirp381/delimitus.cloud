"""SSC-091 probe runner: where does Pulumi spend 3.3 s a rule?

Cloud DNS creates a rule in 0.2 s and takes 660 a minute (``../results-2026-10-06.json``), yet
``pulumi up`` made 18 a minute. This creates N rules shaped like the cell's sinkhole rules
(``program.py``) with ``pulumi up --parallel P`` for each P you give, times the ``up`` and the
``destroy`` that follows, and for P=8 keeps the engine's and the provider's debug log and
summarises it.

    python3 run.py --dry-run                  # the commands, nothing run
    python3 run.py --parallel 1,8,32          # 100 rules each; writes results.json

Standard library only. Only project ``ssc-platform-0``, a policy and rules named ``ssc-exp091p*``,
no network attached. State is a local file backend in a fresh temporary folder for each value of P,
with an empty passphrase: nothing goes to Pulumi Cloud or the repo's stacks. The destroy runs in
``finally``; if it fails the folder is kept and named, and ``python3 ../sinkhole.py
--cleanup-only`` deletes every ``ssc-exp091*`` policy and rule.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

import logsummary

HERE: Final = Path(__file__).resolve().parent
PROJECT: Final = "ssc-platform-0"
FORBIDDEN_PROJECT: Final = "ristretto-506621"
STACK: Final = "probe"
LOG_PARALLEL: Final = 8
"""Only this run keeps the debug log."""
MAX_COUNT: Final = 1500
LOCK: Final = HERE / "uv.lock"
RESULTS: Final = HERE / "results.json"
LOG: Final = HERE / "logs" / "p8.log"


class ProbeError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Done:
    code: int
    out: str
    err: str


class Shell(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        stderr_to: Path | None = None,
    ) -> Done: ...


def guard(argv: Sequence[str], env: Mapping[str, str]) -> None:
    if any(FORBIDDEN_PROJECT in a for a in argv) or any(
        FORBIDDEN_PROJECT in v for v in env.values()
    ):
        raise ProbeError(f"refusing to touch {FORBIDDEN_PROJECT}")


def check_project(project: str) -> str:
    if project != PROJECT:
        raise ProbeError(f"the probe only touches {PROJECT}, not {project!r}")
    return project


def shell(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, stderr_to: Path | None = None
) -> Done:
    guard(argv, env)
    if stderr_to is None:
        done = subprocess.run(  # noqa: S603
            list(argv), env=dict(env), cwd=cwd, capture_output=True, text=True, check=False
        )
        return Done(done.returncode, done.stdout, done.stderr)
    stderr_to.parent.mkdir(parents=True, exist_ok=True)
    with stderr_to.open("a") as log:
        done = subprocess.run(  # noqa: S603
            list(argv), env=dict(env), cwd=cwd, stdout=subprocess.PIPE, stderr=log, text=True,
            check=False,
        )  # fmt: skip
    return Done(done.returncode, done.stdout, "")


def gcloud_token() -> str:
    done = subprocess.run(  # noqa: S603
        ["gcloud", "auth", "print-access-token"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def parallel_values(text: str) -> list[int]:
    try:
        values = [int(v) for v in text.split(",")]
    except ValueError:
        raise ProbeError(f"--parallel wants whole numbers like 1,8,32, not {text!r}") from None
    if not values or any(not 1 <= v <= 256 for v in values) or len(set(values)) != len(values):  # noqa: PLR2004
        raise ProbeError("--parallel wants distinct values from 1 to 256")
    return values


def without_pulumi(base: Mapping[str, str]) -> dict[str, str]:
    """Nothing of the caller's Pulumi login (backend URL, Pulumi Cloud token, passphrase) is kept,
    except ``PULUMI_HOME``, where the installed provider plugins are."""
    return {k: v for k, v in base.items() if not k.startswith("PULUMI_") or k == "PULUMI_HOME"}


def environment(
    base: Mapping[str, str], backend: Path, token: str | None, *, debug: bool
) -> dict[str, str]:
    """A file backend with an empty passphrase, the access token, and for the logged run Terraform's
    debug log (which the provider's stderr carries into Pulumi's)."""
    env = without_pulumi(base)
    env |= {
        "PULUMI_BACKEND_URL": f"file://{backend}",
        "PULUMI_CONFIG_PASSPHRASE": "",
        "PULUMI_SKIP_UPDATE_CHECK": "true",
    }
    if token is not None:
        env["GOOGLE_OAUTH_ACCESS_TOKEN"] = token
    if debug:
        env["TF_LOG"] = "DEBUG"
    return env


def init_args() -> list[str]:
    return ["pulumi", "stack", "init", STACK, "--non-interactive"]


def config_args(key: str, value: str) -> list[str]:
    return ["pulumi", "config", "set", key, value, "--stack", STACK, "--non-interactive"]


def up_args(parallel: int, *, debug: bool) -> list[str]:
    args = [
        "pulumi",
        "up",
        "--yes",
        "--skip-preview",
        "--parallel",
        str(parallel),
        "--stack",
        STACK,
        "--non-interactive",
    ]
    return [*args, "--logflow", "-v=9", "--logtostderr"] if debug else args


def destroy_args(parallel: int, *, debug: bool) -> list[str]:
    args = [
        "pulumi",
        "destroy",
        "--yes",
        "--skip-preview",
        "--parallel",
        str(parallel),
        "--stack",
        STACK,
        "--non-interactive",
    ]
    return [*args, "--logflow", "-v=9", "--logtostderr"] if debug else args


def plan(parallel: Sequence[int], count: int) -> list[str]:
    lines = [
        f"project {PROJECT}; {count} rules + 1 sinkhole rule + 1 policy, named ssc-exp091p*; "
        "nothing is run",
        "environment: PULUMI_BACKEND_URL=file://<new temporary folder>  PULUMI_CONFIG_PASSPHRASE=''  "
        "PULUMI_SKIP_UPDATE_CHECK=true  GOOGLE_OAUTH_ACCESS_TOKEN=<gcloud auth print-access-token>",
        "before: pulumi version; pulumi plugin ls; pulumi and pulumi-gcp versions from uv.lock",
    ]
    for p in parallel:
        debug = p == LOG_PARALLEL
        lines.append(
            f"P={p} (cwd {HERE})" + (f"  [debug log: {LOG}, TF_LOG=DEBUG]" if debug else "")
        )
        for argv in (
            init_args(),
            config_args("project", PROJECT),
            config_args("count", str(count)),
            up_args(p, debug=debug),
            destroy_args(p, debug=debug),
        ):
            lines.append("  $ " + " ".join(argv))
    return lines


def locked_version(lock: str, package: str) -> str:
    """The version ``uv.lock`` pins, which Pulumi's uv toolchain installs into the probe's own
    ``.venv``."""
    found = re.search(rf'\[\[package\]\]\nname = "{re.escape(package)}"\nversion = "([^"]+)"', lock)
    return found.group(1) if found else "unknown"


def versions(sh: Shell, env: Mapping[str, str], lock: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    cli = sh(["pulumi", "version"], env=env, cwd=HERE)
    found["pulumi_cli"] = (
        cli.out.strip() if cli.code == 0 else f"unknown ({cli.err.strip()[-200:]})"
    )
    text = lock.read_text()
    found["pulumi_sdk"] = locked_version(text, "pulumi")
    found["pulumi_gcp"] = locked_version(text, "pulumi-gcp")
    plugins = sh(["pulumi", "plugin", "ls"], env=env, cwd=HERE)
    found["gcp_plugin"] = next(
        (" ".join(line.split()[:3]) for line in plugins.out.splitlines() if " gcp " in f" {line} "),
        "not listed",
    )
    return found


def started_creating(done: Done) -> bool:
    """Whether ``pulumi up`` got as far as creating a resource. Its progress lines say
    ``creating``; an error before that (the language host, the program, the credentials) is a
    startup failure, and another P would fail the same way."""
    return "creating" in done.out


def tail(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()[-1500:]
    except OSError:
        return ""


def one_run(
    sh: Shell,
    *,
    parallel: int,
    count: int,
    token: str | None,
    base_env: Mapping[str, str],
    clock: Callable[[], float],
    say: Callable[[str], None],
    log: Path,
    make_dir: Callable[[], Path],
) -> dict[str, Any]:
    """One value of P on a fresh backend. The destroy runs in ``finally``."""
    debug = parallel == LOG_PARALLEL
    backend = make_dir()
    env = environment(base_env, backend, token, debug=debug)
    plain_env = environment(base_env, backend, token, debug=False)
    result: dict[str, Any] = {"parallel": parallel, "count": count, "rules": count + 1}
    created = False
    kept = False
    try:
        for argv in (
            init_args(),
            config_args("project", PROJECT),
            config_args("count", str(count)),
        ):
            done = sh(argv, env=plain_env, cwd=HERE)
            if done.code:
                raise ProbeError(f"{' '.join(argv[:3])} failed: {done.err.strip()[-500:]}")
            created = created or argv == init_args()
        say(f"P={parallel}: pulumi up ({count + 1} rules)" + (f", log to {log}" if debug else ""))
        began = clock()
        done = sh(
            up_args(parallel, debug=debug), env=env, cwd=HERE, stderr_to=log if debug else None
        )
        result["up_seconds"] = round(clock() - began, 2)
        result["up_ok"] = done.code == 0
        if done.code == 0:
            result["up_rules_per_minute"] = round((count + 1) / result["up_seconds"] * 60, 1)
            say(
                f"P={parallel}: up {result['up_seconds']} s, "
                f"{result['up_rules_per_minute']} a minute"
            )
        else:
            # With the debug log, stderr is in the file: the error is at its end.
            said = done.err.strip() or tail(log) if debug else done.err.strip()
            result["error"] = f"pulumi up exited {done.code}: {said[-500:]}"
            if not started_creating(done):
                result["startup_failure"] = True
            say(f"P={parallel}: up FAILED after {result['up_seconds']} s: {said[-300:]}")
        if debug and log.exists():
            result["log"] = str(log)
            for line in logsummary.report(
                logsummary.summarise(log.read_text(errors="replace").splitlines())
            ):
                say(line)
    except ProbeError as exc:
        result["error"] = str(exc)
    finally:
        if created:
            say(f"P={parallel}: pulumi destroy")
            began = clock()
            done = sh(
                destroy_args(parallel, debug=debug), env=env, cwd=HERE,
                stderr_to=log if debug else None,
            )  # fmt: skip
            result["destroy_seconds"] = round(clock() - began, 2)
            result["destroy_ok"] = done.code == 0
            result["destroy_rules_per_minute"] = round(
                (count + 1) / max(result["destroy_seconds"], 0.01) * 60, 1
            )
            say(f"P={parallel}: destroy {result['destroy_seconds']} s")
            if done.code:
                kept = True
                result["error"] = (result.get("error", "") + " ").lstrip() + (
                    f"pulumi destroy exited {done.code}: {done.err.strip()[-500:]}; state kept in "
                    f"{backend}; run python3 ../sinkhole.py --cleanup-only"
                )
                say(f"P={parallel}: DESTROY FAILED. State kept in {backend}. {result['error']}")
        if not kept:
            shutil.rmtree(backend, ignore_errors=True)
    return result


def main(
    argv: Sequence[str],
    *,
    sh: Shell = shell,
    clock: Callable[[], float] = time.monotonic,
    token_source: Callable[[], str] = gcloud_token,
    say: Callable[[str], None] = lambda text: print(text, flush=True),  # noqa: T201
    base_env: Mapping[str, str] | None = None,
    results: Path = RESULTS,
    log: Path = LOG,
    lock: Path = LOCK,
    make_dir: Callable[[], Path] = lambda: Path(tempfile.mkdtemp(prefix="ssc-exp091p-")),
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", maxsplit=1)[0])
    parser.add_argument("--parallel", default="1,8,32", help="comma separated, default 1,8,32")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        values = parallel_values(args.parallel)
        if not 1 <= args.count <= MAX_COUNT:
            raise ProbeError(f"--count is 1 to {MAX_COUNT}")
        check_project(PROJECT)
        if args.dry_run:
            for line in plan(values, args.count):
                say(line)
            return 0
        if not lock.exists():
            raise ProbeError(f"{lock} is missing: the probe's pinned versions (uv lock)")
        env_base = dict(os.environ if base_env is None else base_env)
        token = env_base.get("GOOGLE_OAUTH_ACCESS_TOKEN") or token_source()
    except (ProbeError, subprocess.CalledProcessError, OSError) as exc:
        say(str(exc))
        return 2
    report: dict[str, Any] = {
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": PROJECT,
        "count": args.count,
        "runs": [],
    }
    failed = False
    try:
        report |= versions(sh, without_pulumi(env_base), lock)
        say(
            f"pulumi {report['pulumi_cli']}; pulumi-gcp {report['pulumi_gcp']}; plugin {report['gcp_plugin']}"
        )
        for p in values:
            run = one_run(
                sh, parallel=p, count=args.count, token=token, base_env=env_base, clock=clock,
                say=say, log=log, make_dir=make_dir,
            )  # fmt: skip
            report["runs"].append(run)
            failed = failed or "error" in run
            results.write_text(json.dumps(report, indent=2) + "\n")
            if run.get("startup_failure"):
                say(
                    f"stopping after P={p}: pulumi up failed before creating anything, so the "
                    "other values would fail the same way. Fix it and run again."
                )
                break
    except ProbeError as exc:
        say(str(exc))
        failed = True
    finally:
        results.write_text(json.dumps(report, indent=2) + "\n")
    say(f"results in {results}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
