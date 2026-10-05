"""The cell deployer (SSC-087): turns on one lazy resource of one cell by applying that cell's
stack with the resource's flag set, and changes nothing else. It also sets the gateway's part of
the warm option (SSC-092), the ``warm`` flag, to true or false.

    python -I -m ssc_infra.deployer <cell label> <database|egress|connections|warm=true|warm=false>

It runs as the Cloud Run job ``ssc-cell-deployer`` in ``ssc-platform-0``, under its own identity.
The control plane may start it with its own arguments and environment, so it takes exactly two
arguments from fixed sets, refuses any environment variable it does not expect, and runs Pulumi
with an environment of its own. The config applied is the one the stack was last applied with
(``stack_config``) with the one flag set to true, or ``warm`` set as asked; nothing here sets any
other flag to false.

A run killed halfway is converged by the next: a lock older than any run is cancelled, and a
create Pulumi never saw finish is imported where its ID is known, dropped otherwise.
"""

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from ssc_infra import naming as n
from ssc_infra import stack_config
from ssc_infra.cell import PROXY_ZONE
from ssc_infra.run import CommandError, pulumi
from ssc_infra.stack_config import INFRA_DIR, Pulumi
from ssc_shared.hosts import check_cell_label

ENV_ALLOWED: Final = frozenset(
    {
        "HOME",
        "HOSTNAME",
        "PATH",
        "PWD",
        "LANG",
        "LC_CTYPE",
        "GPG_KEY",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "CLOUD_RUN_JOB",
        "CLOUD_RUN_EXECUTION",
        "CLOUD_RUN_TASK_INDEX",
        "CLOUD_RUN_TASK_ATTEMPT",
        "CLOUD_RUN_TASK_COUNT",
    }
)
PULUMI_ENV: Final = {
    "PATH": f"{INFRA_DIR}/.venv/bin:/opt/pulumi:/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/ssc",
    "PULUMI_HOME": "/app/.pulumi",
    "PULUMI_BACKEND_URL": f"gs://{n.STATE_BUCKET}",
    "PULUMI_SKIP_UPDATE_CHECK": "true",
    "UV_NO_SYNC": "1",
    "UV_OFFLINE": "1",
    "UV_FROZEN": "1",
    "UV_NO_DEV": "true",
    "UV_PYTHON_DOWNLOADS": "never",
    "UV_CACHE_DIR": "/home/ssc/.cache/uv",
}
STALE_LOCK: Final = timedelta(hours=3)
IMPORT_IDS: Final = {
    "gcp:sql/databaseInstance:DatabaseInstance::sql": "projects/{project}/instances/ssc-cell",
    "gcp:compute/instanceGroupManager:InstanceGroupManager::proxy": (
        f"projects/{{project}}/zones/{PROXY_ZONE}/instanceGroupManagers/ssc-proxy"
    ),
    f"gcp:cloudrunv2/service:Service::{n.DATA_GATEWAY}": (
        f"projects/{{project}}/locations/{n.REGION}/services/{n.DATA_GATEWAY}"
    ),
}

type Locks = Callable[[str], list[datetime]]


class RefusedError(ValueError):
    pass


def parse(argv: Sequence[str], environ: Mapping[str, str]) -> tuple[str, str]:
    """The cell label and the flag, or ``RefusedError``."""
    unexpected = sorted(set(environ) - ENV_ALLOWED)
    if unexpected:
        raise RefusedError(f"unexpected environment: {', '.join(unexpected)}")
    if len(argv) != 2:  # noqa: PLR2004  (a label and a flag)
        raise RefusedError("expected exactly: <cell label> <flag>")
    label, flag = argv
    if flag not in n.LAZY_FLAGS and flag not in n.WARM_ARGS:
        raise RefusedError(f"the flag must be one of {', '.join((*n.LAZY_FLAGS, *n.WARM_ARGS))}")
    try:
        check_cell_label(label)
    except ValueError as exc:
        raise RefusedError(str(exc)) from None
    return label, flag


def setting(flag: str) -> dict[str, str]:
    """The one config value a run changes: a lazy flag to true, or ``warm`` as asked."""
    if flag in n.WARM_ARGS:
        return {"warm": n.WARM_ARGS[flag]}
    return {flag: "true"}


def clean_pulumi(*args: str, cwd: str | None = None) -> str:
    return pulumi(*args, cwd=cwd, env=PULUMI_ENV)


def state_locks(stack: str) -> list[datetime]:
    """When each lock on the stack in the state bucket was taken."""
    from ssc_shared.blobstore_gcs import bucket_of  # noqa: PLC0415

    bucket = bucket_of(n.STATE_BUCKET, project=n.BOOTSTRAP_PROJECT)
    prefix = f".pulumi/locks/organization/{n.PROJECT}/{stack}/"
    blobs = bucket.list_blobs(prefix=prefix, timeout=30.0)
    return [b.time_created for b in blobs if b.time_created is not None]


def import_id(urn: str, label: str) -> str | None:
    """The cloud ID of a lazy resource from its URN, where its name is fixed."""
    *_, type_, name = urn.split("::")
    template = IMPORT_IDS.get(f"{type_.rsplit('$', 1)[-1]}::{name}")
    return template.format(project=n.cell_project(label)) if template else None


@dataclass(frozen=True, slots=True, kw_only=True)
class Deployer:
    """Everything a run calls out to; tests give fakes."""

    run: Pulumi = clean_pulumi
    locks: Locks = state_locks
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    infra_dir: str = INFRA_DIR

    def apply(self, label: str, flag: str) -> None:
        stack = n.cell_stack(label)
        settings = stack_config.applied(label, run=self.run, infra_dir=self.infra_dir)
        stack_config.write(label, settings | setting(flag), run=self.run, infra_dir=self.infra_dir)
        self.unlock(stack)
        self.recover(stack, label)
        self.run("up", "--yes", "--stack", stack, cwd=self.infra_dir)

    def unlock(self, stack: str) -> None:
        """A lock older than ``STALE_LOCK`` was left by a killed run; a newer one is a live run."""
        taken = self.locks(stack)
        if not taken:
            return
        now = self.now()
        if any(now - t < STALE_LOCK for t in taken):
            raise CommandError(f"{stack} is being applied by another run")
        self.run("cancel", "--yes", "--stack", stack, cwd=self.infra_dir)

    def recover(self, stack: str, label: str) -> None:
        """Imports or drops every create a killed run left pending."""
        state: Any = json.loads(self.run("stack", "export", "--stack", stack, cwd=self.infra_dir))
        ops: list[dict[str, Any]] = state.get("deployment", {}).get("pending_operations") or []
        pending = [str(op["resource"]["urn"]) for op in ops if op.get("type") == "creating"]
        known = {urn: found for urn in pending if (found := import_id(urn, label))}
        if known:
            pairs = [a for kv in known.items() for v in kv for a in ("--import-pending-creates", v)]
            targets = [a for u in known for a in ("--target", u)]
            self.run("refresh", "--yes", "--stack", stack, *targets, *pairs, cwd=self.infra_dir)
        if unknown := [u for u in pending if u not in known]:
            targets = [a for u in unknown for a in ("--target", u)]
            self.run(
                "refresh",
                "--yes",
                "--stack",
                stack,
                "--clear-pending-creates",
                *targets,
                cwd=self.infra_dir,
            )


def main(argv: Sequence[str], environ: Mapping[str, str], deployer: Deployer | None = None) -> int:
    try:
        label, flag = parse(argv, environ)
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    try:
        (deployer or Deployer()).apply(label, flag)
    except CommandError as exc:
        print(exc, file=sys.stderr)  # noqa: T201
        return 1
    ((name, value),) = setting(flag).items()
    print(f"{n.cell_stack(label)}: {name} is {value}", file=sys.stderr)  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))
