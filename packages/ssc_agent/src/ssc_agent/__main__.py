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
the control plane records no usage events. Connection secrets (SSC-051) need both
``SSC_DATA_SA``, the data gateway's service account, and ``SSC_CONNECTION_TAG``, the cell's
connection tag as ``tagKeys/<n>=tagValues/<n>``; with neither the agent refuses them, and one
without the other or a malformed tag exits 2. Egress proxy credentials (SSC-053) need
``SSC_PROXY_ADDRESS``, the proxy's reserved internal address; unset, the agent refuses them.
``SSC_OUTBOUND_IP`` is the cell's fixed outbound address, which ``info`` reports. Either one not
an IPv4 address exits 2.
"""

import ipaddress
import logging
import os
import re
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
from ssc_agent.egress import ProxyCredentials
from ssc_agent.metadata import MetadataAccessTokens
from ssc_agent.secret_manager import CellSecretCustody, CellSecretWriter, ConnectionSecrets
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
DATA_SA_ENV: Final = "SSC_DATA_SA"
CONNECTION_TAG_ENV: Final = "SSC_CONNECTION_TAG"
CONNECTION_TAG: Final = re.compile(r"(tagKeys/[0-9]+)=(tagValues/[0-9]+)")
PROXY_ADDRESS_ENV: Final = "SSC_PROXY_ADDRESS"
OUTBOUND_IP_ENV: Final = "SSC_OUTBOUND_IP"


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


def connections_from_env(env: Mapping[str, str]) -> ConnectionSecrets | None:
    """None when neither is set; ``ConfigError`` when only one is, or the tag is malformed."""
    reader, tag = env.get(DATA_SA_ENV, ""), env.get(CONNECTION_TAG_ENV, "")
    if not reader and not tag:
        return None
    if not reader or not tag:
        raise ConfigError(f"set both {DATA_SA_ENV} and {CONNECTION_TAG_ENV}, or neither")
    m = CONNECTION_TAG.fullmatch(tag)
    if m is None:
        raise ConfigError(f"{CONNECTION_TAG_ENV} is not tagKeys/<n>=tagValues/<n>")
    return ConnectionSecrets(reader=reader, tag_key=m.group(1), tag_value=m.group(2))


def ipv4_from_env(env: Mapping[str, str], name: str) -> str | None:
    """None when unset; ``ConfigError`` for anything but an IPv4 address."""
    value = env.get(name, "")
    if not value:
        return None
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        raise ConfigError(f"{name} is not an IPv4 address") from None
    return value


def main() -> int:
    try:
        cell = cell_from_env(os.environ)
        build = build_config_from_env(os.environ, cell)
        views = log_views_from_env(os.environ)
        usage_source = usage_source_from_env(os.environ)
        connections = connections_from_env(os.environ)
        proxy_address = ipv4_from_env(os.environ, PROXY_ADDRESS_ENV)
        outbound_ip = ipv4_from_env(os.environ, OUTBOUND_IP_ENV)
    except ConfigError as exc:
        print(f"ssc-agent: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    logging.basicConfig(level=logging.INFO)
    redaction.install()
    tokens = MetadataAccessTokens()
    builder = None if build is None else CloudBuildDriver(build, tokens)
    driver = CloudRunDriver(cell, tokens)
    custody = CellSecretCustody(cell, tokens, driver.ensure_identity, connections=connections)
    writer = CellSecretWriter(cell.project, tokens)
    instance = os.environ.get(SQL_INSTANCE_ENV, "")
    databases = None
    if instance:
        sql = CloudSqlAdmin(cell.project, instance, tokens)
        databases = CellAppDatabases(sql, custody, writer)
    egress = ProxyCredentials(custody, writer, proxy_address=proxy_address, outbound_ip=outbound_ip)
    entries = None if views is None else CloudLoggingEntries(views, tokens)
    series = None if usage_source is None else CloudMonitoringSeries(cell.project, tokens)
    if series is None:
        logging.getLogger(__name__).info("usage reads are off: %s is not set", USAGE_SOURCE_ENV)
    hub = CellLogHub(entries, driver)
    app = create_app(
        driver, builder, custody, databases, hub, usage=CellUsageReader(series), egress=egress
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # noqa: S104
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
