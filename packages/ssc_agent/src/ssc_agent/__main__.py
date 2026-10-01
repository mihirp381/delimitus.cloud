"""``python -m ssc_agent``: serve the cell agent on ``$PORT``.

Configuration, all required and set by the cell stack: ``SSC_CELL_PROJECT``,
``SSC_CELL_REGION``, ``SSC_CELL_NETWORK``, ``SSC_CELL_SUBNETWORK``, ``SSC_IMAGE_REPOSITORY``,
``SSC_GATEWAY_SA``. Exits 2 when one is missing.
"""

import os
import sys
from collections.abc import Mapping
from typing import Final

import uvicorn

from ssc_agent.app import create_app
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


class ConfigError(ValueError):
    pass


def cell_from_env(env: Mapping[str, str]) -> CellRuntime:
    missing = [name for name in ENV.values() if not env.get(name)]
    if missing:
        raise ConfigError(f"missing {', '.join(missing)}")
    return CellRuntime(**{field: env[name] for field, name in ENV.items()})


def main() -> int:
    try:
        cell = cell_from_env(os.environ)
    except ConfigError as exc:
        print(f"ssc-agent: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    app = create_app(CloudRunDriver(cell, MetadataAccessTokens()))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # noqa: S104
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
