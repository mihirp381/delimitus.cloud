"""A cell stack's config as it was last applied, written back to its stack file (SSC-087).

Stack files are not committed and the cell deployer starts from a clean image, so each cell stack
exports every setting as ``config``. ``restore`` writes those settings back with the KMS secrets
provider, replacing the local stack file. The operator runs it after the control plane has turned
a flag on, so a run from their checkout keeps that flag:

    uv run python -m ssc_infra.stack_config <cell label>
"""

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Protocol, cast

from ssc_infra import naming as n
from ssc_infra.run import CommandError, pulumi

INFRA_DIR: Final = str(Path(__file__).resolve().parent.parent)
GLOBAL_WARNING: Final = "gcp:disableGlobalProjectWarning"


class Pulumi(Protocol):
    def __call__(self, *args: str, cwd: str | None = None) -> str: ...


def applied(label: str, *, run: Pulumi = pulumi, infra_dir: str = INFRA_DIR) -> dict[str, str]:
    """The ``config`` output of the stack's last apply. A stack applied before SSC-087 has none
    and must be applied once by hand first."""
    stack = n.cell_stack(label)
    outputs: Any = json.loads(run("stack", "output", "--json", "--stack", stack, cwd=infra_dir))
    config: Any = (
        cast("dict[str, Any]", outputs).get("config") if isinstance(outputs, dict) else None
    )
    if not isinstance(config, dict) or not config:
        raise CommandError(f"{stack} records no applied config: apply it once by hand")
    return {str(k): str(v) for k, v in cast("dict[object, object]", config).items()}


def write(
    label: str, settings: Mapping[str, str], *, run: Pulumi = pulumi, infra_dir: str = INFRA_DIR
) -> None:
    """Replaces the stack file with exactly ``settings``."""
    stack = n.cell_stack(label)
    (Path(infra_dir) / f"Pulumi.{stack}.yaml").write_text(
        f"secretsprovider: {n.SECRETS_PROVIDER}\n"
    )
    pairs = {GLOBAL_WARNING: "true"} | dict(settings)
    plain = [arg for k, v in pairs.items() for arg in ("--plaintext", f"{k}={v}")]
    run("config", "set-all", "--stack", stack, *plain, cwd=infra_dir)


def restore(label: str, *, run: Pulumi = pulumi, infra_dir: str = INFRA_DIR) -> dict[str, str]:
    settings = applied(label, run=run, infra_dir=infra_dir)
    write(label, settings, run=run, infra_dir=infra_dir)
    return settings


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)  # noqa: T201
        return 2
    try:
        settings = restore(argv[0])
    except (CommandError, ValueError) as exc:
        print(exc, file=sys.stderr)  # noqa: T201
        return 1
    print(json.dumps(settings, indent=2))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
