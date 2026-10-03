"""``python -m ssc_agent``: serve the cell agent on ``$PORT``.

Configuration, all required and set by the cell stack: ``SSC_CELL_PROJECT``,
``SSC_CELL_REGION``, ``SSC_CELL_NETWORK``, ``SSC_CELL_SUBNETWORK``, ``SSC_IMAGE_REPOSITORY``,
``SSC_GATEWAY_SA``. Builds (SSC-015) need all of ``SSC_BUILD_SA``, ``SSC_BUILD_TOOLS_IMAGE`` and
``SSC_BUILD_FRONTEND_IMAGE``, or none, and then the agent refuses builds. Exits 2 when one is
missing or malformed.
"""

import os
import sys
from collections.abc import Mapping
from typing import Final

import uvicorn

from ssc_agent.app import create_app
from ssc_agent.cloud_build import CellBuildConfig, CloudBuildDriver
from ssc_agent.cloud_run import CellRuntime, CloudRunDriver
from ssc_agent.metadata import MetadataAccessTokens

ENV: Final = {
    "project": "SSC_CELL_PROJECT",
    "region": "SSC_CELL_REGION",
    "network": "SSC_CELL_NETWORK",
    "subnetwork": "SSC_CELL_SUBNETWORK",
    "image_repository": "SSC_IMAGE_REPOSITORY",
    "invoker": "SSC_GATEWAY_SA",
}
BUILD_ENV: Final = {
    "service_account": "SSC_BUILD_SA",
    "tools_image": "SSC_BUILD_TOOLS_IMAGE",
    "frontend_image": "SSC_BUILD_FRONTEND_IMAGE",
}


class ConfigError(ValueError):
    pass


def cell_from_env(env: Mapping[str, str]) -> CellRuntime:
    missing = [name for name in ENV.values() if not env.get(name)]
    if missing:
        raise ConfigError(f"missing {', '.join(missing)}")
    return CellRuntime(**{field: env[name] for field, name in ENV.items()})


def build_config_from_env(env: Mapping[str, str], cell: CellRuntime) -> CellBuildConfig | None:
    """None when no build variable is set; ``ConfigError`` when only some are, or one is bad."""
    missing = [name for name in BUILD_ENV.values() if not env.get(name)]
    if len(missing) == len(BUILD_ENV):
        return None
    if missing:
        raise ConfigError(f"missing {', '.join(missing)}")
    try:
        return CellBuildConfig(
            project=cell.project,
            region=cell.region,
            image_repository=cell.image_repository,
            **{field: env[name] for field, name in BUILD_ENV.items()},
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def main() -> int:
    try:
        cell = cell_from_env(os.environ)
        build = build_config_from_env(os.environ, cell)
    except ConfigError as exc:
        print(f"ssc-agent: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    tokens = MetadataAccessTokens()
    builder = None if build is None else CloudBuildDriver(build, tokens)
    app = create_app(CloudRunDriver(cell, tokens), builder)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # noqa: S104
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
