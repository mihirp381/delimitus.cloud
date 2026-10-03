"""``python -m ssc_agent``: serve the cell agent on ``$PORT``.

Configuration, all required and set by the cell stack: ``SSC_CELL_PROJECT``,
``SSC_CELL_REGION``, ``SSC_CELL_NETWORK``, ``SSC_CELL_SUBNETWORK``, ``SSC_IMAGE_REPOSITORY``,
``SSC_GATEWAY_SA``. Builds (SSC-015) need all of ``SSC_BUILD_SA``, ``SSC_BUILD_TOOLS_IMAGE`` and
``SSC_BUILD_FRONTEND_IMAGE``, or none, and then the agent refuses builds. Exits 2 when one is
missing or malformed. Secrets (SSC-026) need nothing more: they live in the cell's project and
region. App databases (SSC-040) need ``SSC_SQL_INSTANCE``, the name of the cell's Cloud SQL
instance; unset, the agent refuses them. App logs (SSC-024) need ``SSC_LOG_VIEW``, the full names
of the cell's log views (``projects/<p>/locations/<l>/buckets/<b>/views/<v>``), separated by
commas; unset, the agent refuses log reads and health uses the service alone. Log records are
redacted (``ssc_shared.redaction``). App usage (SSC-028) needs ``SSC_USAGE_SOURCE=monitoring``,
set once the agent may read the cell's Cloud Monitoring; unset, the agent refuses usage reads and
the control plane records no usage events.
"""

import logging
import os
import sys
from collections.abc import Mapping
from typing import Final

import uvicorn

from ssc_agent.app import create_app
from ssc_agent.app_database import CellAppDatabases
from ssc_agent.cloud_build import CellBuildConfig, CloudBuildDriver
from ssc_agent.cloud_logging import LOG_VIEW, CellLogHub, CloudLoggingEntries
from ssc_agent.cloud_monitoring import CellUsageReader, CloudMonitoringSeries
from ssc_agent.cloud_run import CellRuntime, CloudRunDriver
from ssc_agent.cloud_sql import CloudSqlAdmin
from ssc_agent.metadata import MetadataAccessTokens
from ssc_agent.secret_manager import CellSecretCustody, CellSecretWriter
from ssc_shared import redaction

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
SQL_INSTANCE_ENV: Final = "SSC_SQL_INSTANCE"
LOG_VIEW_ENV: Final = "SSC_LOG_VIEW"
USAGE_SOURCE_ENV: Final = "SSC_USAGE_SOURCE"
USAGE_SOURCES: Final = ("monitoring",)


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


def log_views_from_env(env: Mapping[str, str]) -> tuple[str, ...] | None:
    """None when unset; ``ConfigError`` unless every comma-separated name is a log view."""
    value = env.get(LOG_VIEW_ENV, "")
    if not value:
        return None
    views = tuple(view.strip() for view in value.split(","))
    if any(LOG_VIEW.fullmatch(view) is None for view in views):
        raise ConfigError(f"{LOG_VIEW_ENV} is not a list of log view names")
    return views


def usage_source_from_env(env: Mapping[str, str]) -> str | None:
    """None when unset; ``ConfigError`` for a source the agent cannot read."""
    value = env.get(USAGE_SOURCE_ENV, "")
    if not value:
        return None
    if value not in USAGE_SOURCES:
        raise ConfigError(f"{USAGE_SOURCE_ENV} is one of {', '.join(USAGE_SOURCES)}")
    return value


def main() -> int:
    try:
        cell = cell_from_env(os.environ)
        build = build_config_from_env(os.environ, cell)
        views = log_views_from_env(os.environ)
        usage_source = usage_source_from_env(os.environ)
    except ConfigError as exc:
        print(f"ssc-agent: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    logging.basicConfig(level=logging.INFO)
    redaction.install()
    tokens = MetadataAccessTokens()
    builder = None if build is None else CloudBuildDriver(build, tokens)
    driver = CloudRunDriver(cell, tokens)
    custody = CellSecretCustody(cell, tokens, driver.ensure_identity)
    instance = os.environ.get(SQL_INSTANCE_ENV, "")
    databases = None
    if instance:
        sql = CloudSqlAdmin(cell.project, instance, tokens)
        writer = CellSecretWriter(cell.project, tokens)
        databases = CellAppDatabases(sql, custody, writer)
    entries = None if views is None else CloudLoggingEntries(views, tokens)
    series = None if usage_source is None else CloudMonitoringSeries(cell.project, tokens)
    if series is None:
        logging.getLogger(__name__).info("usage reads are off: %s is not set", USAGE_SOURCE_ENV)
    hub = CellLogHub(entries, driver)
    app = create_app(driver, builder, custody, databases, hub, usage=CellUsageReader(series))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # noqa: S104
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
