"""A ``c-<cell label>`` stack: one customer cell, one project (SSC-013, decisions 021 and 022).

Everything a cell holds is named from the label alone, so two cells differ only in their label,
project number, addresses and the resources their flags name; ``python -m ssc_infra.cell_diff``
checks exactly that.
"""

import base64
import binascii
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import ip_network
from pathlib import Path
from typing import Final, cast

import pulumi
import pulumi_gcp as gcp

from ssc_infra import alerts
from ssc_infra import naming as n
from ssc_infra.control import PINNED_IMAGE, PLACEHOLDER_IMAGE, public_jwks_kids
from ssc_infra.platform import (
    BUDGET_THRESHOLDS,
    TAG_USER,
    ZONE_RECORD_PERMISSIONS,
    provider,
    sa_principal,
)
from ssc_shared.runtime import (
    CONNECTION_ID,
    CONNECTION_SECRET_PREFIX,
    SECRET_VERSION,
    connection_env,
    connection_secret_id,
    service_name,
)

APIS: Final = (
    "artifactregistry.googleapis.com",
    "certificatemanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudkms.googleapis.com",
    # Answers testIamPermissions, which the metadata_token_no_roles probe asks as the app.
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "dns.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
)
SUBNETS: Final = {"apps": "10.20.0.0/24", "gateway": "10.20.4.0/24"}
APPS_SUBNET: Final = "apps"
EDGE_SUBNET: Final = "gateway"
PROXY_HOST: Final = 10
DATAGW_HOST: Final = 11
PROXY_PORT: Final = 3128
PROXY_TAG: Final = "ssc-proxy"
DATA_TAG: Final = "ssc-data"
PROXY_MACHINE: Final = "e2-micro"
PROXY_ZONE: Final = f"{n.REGION}-a"
PROXY_BOOT_IMAGE: Final = "cos-cloud/cos-stable"
PROXY_SA: Final = "ssc-proxy"
PROXY_UNIT: Final = "ssc-egress.service"
PROXY_HA_SIZE: Final = 2
PROXY_HEAL_DELAY: Final = 300
PROXY_CHECK_SECONDS: Final = 10
PROXY_STOP_SECONDS: Final = 15
PRIVATE_RANGES: Final = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
PROXY_HOST_RANGES: Final = ("169.254.0.0/16", "127.0.0.0/8")
"""Refused on the proxy machine itself for tunnels: the VPC firewall never sees traffic to the
metadata server or the machine's own loopback."""
TUNNEL_PORT: Final = 443
HEALTH_CHECK_RANGES: Final = ("35.191.0.0/16", "130.211.0.0/22")
GATEWAY_CONCURRENCY: Final = 1000
ENTRY_TIMEOUT_SECONDS: Final = 3600
GATEWAY_TIMEOUT: Final = f"{ENTRY_TIMEOUT_SECONDS}s"
ENTRY: Final = "ssc-entry"
LB_SCHEME: Final = "EXTERNAL_MANAGED"
TLS_MIN: Final = "TLS_1_2"
TLS_PROFILE: Final = "MODERN"
AGENT_PATHS: Final = "agent"
INTAKE_PATHS: Final = "intake"
DNS_TTL: Final = 300
CELL_BUDGET_USD: Final = 50
PSA_ADDRESS: Final = "10.21.0.0"
PSA_PREFIX: Final = 20
GOOGLE_PRIVATE: Final = ("199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11")
GOOGLE_PRIVATE_RANGE: Final = "199.36.153.8/30"
GATEWAY_TAG: Final = "ssc-gateway"
AGENT_MAX: Final = 1
AGENT_CONCURRENCY: Final = 200
AGENT_TIMEOUT: Final = "300s"
LOG_BUCKET: Final = "_Default"
LOG_VIEWS: Final = {
    "ssc-run": 'resource.type = "cloud_run_revision"',
    "ssc-build": 'resource.type = "build"',
}
LOG_VIEW_ENV: Final = "SSC_LOG_VIEW"
PROXY_ADDRESS_ENV: Final = "SSC_PROXY_ADDRESS"
OUTBOUND_IP_ENV: Final = "SSC_OUTBOUND_IP"
BUCKET_ENV: Final = "SSC_CELL_BUCKET"
USAGE_SOURCE_ENV: Final = "SSC_USAGE_SOURCE"
USAGE_SOURCE: Final = "monitoring"
DATA_SA_ENV: Final = "SSC_DATA_SA"
CONNECTION_TAG_ENV: Final = "SSC_CONNECTION_TAG"
SECRET_KIND_KEY: Final = "ssc-secret-kind"  # noqa: S105
CONNECTION_KIND: Final = "connection"
INTAKE_MAX: Final = 3
INTAKE_COMMAND: Final = ("python", "-m", "ssc_agent.intake")
DATAGW_MAX: Final = 10
KEY_ROTATION: Final = "7776000s"
SQL_TIER: Final = "db-f1-micro"
SQL_INSTANCE: Final = "ssc-cell"
SQL_MAX_CONNECTIONS: Final = "25"
SQL_CA_MODE: Final = "GOOGLE_MANAGED_CAS_CA"
SQL_DNS_ZONE: Final = "sql-psa.goog."
SQL_DNS_NAMES: Final = f"*.{SQL_DNS_ZONE}"
GOOGLE_DNS_PASSTHRU: Final = (
    "googleapis.com.",
    "*.googleapis.com.",
    "run.app.",
    "*.run.app.",
    "metadata.google.internal.",
    "*.google.internal.",
    "*.internal.",
    "pkg.dev.",
    "*.pkg.dev.",
)
SINKHOLE: Final = "192.0.2.1"  # TEST-NET-1: never routed
SINKHOLE_V6: Final = "100::1"  # the discard prefix
SINKHOLE_NAME: Final = "sinkhole.ssc-cell."
# IANA's list of top-level domains (https://data.iana.org/TLD/tlds-alpha-by-domain.txt), as fetched.
TLDS_FILE: Final = Path(__file__).with_name("tlds.txt")
APP_IMAGE: Final = "apps"
APP_CONDITION: Final = 'resource.name.extract("/{kind}/{{name}}").startsWith("{prefix}")'
SECRET_PREFIXES: Final = (n.APP_PREFIX, CONNECTION_SECRET_PREFIX)
CREATE_PERMISSIONS: Final = (
    "iam.serviceAccounts.create",
    "run.services.create",
    "secretmanager.secrets.create",
    "cloudbuild.builds.create",
)
# Cloud Run and service accounts take no resource-name IAM conditions (only Secret Manager of
# the agent's APIs does), so these are granted on the project and the agent's code holds it to
# ``ssc-a-`` names. Exactly what ``ssc_agent.cloud_run`` and ``ssc_agent.cloud_build`` call; no
# delete.
RUNTIME_PERMISSIONS: Final = (
    "run.services.get",
    "run.services.update",
    "run.services.getIamPolicy",
    "run.services.setIamPolicy",
    "run.revisions.get",
    "run.revisions.list",
    "iam.serviceAccounts.actAs",
    "cloudbuild.builds.get",
    "cloudbuild.builds.list",
)
DATABASE_PERMISSIONS: Final = (
    "cloudsql.instances.executeSql",
    "cloudsql.instances.login",
    "cloudsql.instances.get",
    "cloudsql.instances.listServerCas",
    "cloudsql.databases.create",
    "cloudsql.databases.delete",
)
USAGE_PERMISSIONS: Final = ("monitoring.timeSeries.list",)
FILES_PERMISSIONS: Final = ("storage.objects.delete",)
FILES_PREFIX: Final = "files/"
SNAPSHOTS_PREFIX: Final = "snapshots/"
NONCURRENT_FILE_DAYS: Final = 7
BUILD_IMAGES: Final = ("build_tools_image", "build_frontend_image")
GATEWAY_SETTINGS: Final = ("gateway_image", "gateway_keyring", "gateway_jwks", "org_id")
MAX_TIMER_KEYS: Final = 2
CUSTOMER_ORG: Final = re.compile(r"org_[a-z0-9]{20}")


@dataclass(frozen=True, slots=True)
class CellConfig:
    label: str
    stage: n.Stage
    probe: bool
    gateway_min: int
    gateway_max: int
    agent_image: str | None
    probe_digest: str | None
    billing_account: str
    database: bool
    egress: bool
    connections: bool
    warm: bool
    build_tools_image: str | None = None
    build_frontend_image: str | None = None
    gateway_image: str | None = None
    gateway_keyring: str | None = None
    gateway_jwks: str | None = None
    org_id: str | None = None
    timer_jwks: str | None = None
    """The control plane's public timer JWKS (SSC-041); unset, the gateway refuses timer
    calls."""
    datagw_image: str | None = None
    datagw_connections: str | None = None
    """The connections ``ssc-datagw`` mounts (SSC-051), as ``datagw_connections_setting``
    returns them."""
    proxy_image: str | None = None
    """The egress proxy (SSC-053), a build of ``packages/ssc_egress/Dockerfile``."""
    proxy_ha: bool = False
    """With ``egress``, two proxy machines in two zones behind an internal load balancer at the
    reserved address: the paid option, off by default (SSC-053)."""
    oncall_email: str | None = None
    """Where the cell's alerts go (SSC-062); unset, the cell has no alert resources."""

    @property
    def project_id(self) -> str:
        return n.cell_project(self.label)

    @property
    def disposable(self) -> bool:
        return self.stage == "staging"

    @property
    def gateway_floor(self) -> int:
        """The gateway's minimum instances: ``warm`` keeps at least one."""
        return max(self.gateway_min, 1) if self.warm else self.gateway_min

    @property
    def flags(self) -> dict[str, bool | int]:
        return {
            "database": self.database,
            "egress": self.egress,
            "connections": self.connections,
            "gateway_min": self.gateway_min,
            "warm": self.warm,
            "proxy_ha": self.proxy_ha,
        }

    @property
    def settings(self) -> dict[str, str]:
        """Every setting as ``pulumi config set`` takes it, so the deployer can restore the
        config this stack was last applied with (SSC-087). No setting is a secret: the gateway
        keyring is KMS ciphertext only the gateway may decrypt."""
        values: dict[str, str | int | bool | None] = {
            "stage": self.stage,
            "probe": self.probe,
            "gateway_max": self.gateway_max,
            "agent_image": self.agent_image,
            "probe_digest": self.probe_digest,
            "billing_account": self.billing_account,
            "build_tools_image": self.build_tools_image,
            "build_frontend_image": self.build_frontend_image,
            "gateway_image": self.gateway_image,
            "gateway_keyring": self.gateway_keyring,
            "gateway_jwks": self.gateway_jwks,
            "org_id": self.org_id,
            "timer_jwks": self.timer_jwks,
            "datagw_image": self.datagw_image,
            "datagw_connections": self.datagw_connections,
            "proxy_image": self.proxy_image,
            "oncall_email": self.oncall_email,
            **self.flags,
        }
        return {
            k: str(v).lower() if isinstance(v, bool) else str(v)
            for k, v in sorted(values.items())
            if v is not None
        }


def read_config(stack: str) -> CellConfig:
    config = pulumi.Config()
    stage = config.get("stage") or "staging"
    if stage != "prod" and stage != "staging":  # noqa: PLR1714  (narrows to Stage)
        raise ValueError(f"stage must be one of {n.STAGES}, not {stage!r}")
    tools, frontend = build_images(config.get(BUILD_IMAGES[0]), config.get(BUILD_IMAGES[1]))
    image, keyring, jwks, org = gateway_settings(*(config.get(key) for key in GATEWAY_SETTINGS))
    datagw_image = datagw_settings(config.get("datagw_image"), org)
    return CellConfig(
        label=n.label_of_stack(stack),
        stage=stage,
        probe=config.get_bool("probe") or False,
        gateway_min=_int_or(config.get_int("gateway_min"), 0),
        gateway_max=_int_or(config.get_int("gateway_max"), 20),
        agent_image=config.get("agent_image"),
        probe_digest=config.get("probe_digest"),
        billing_account=config.get("billing_account") or n.BILLING_ACCOUNT,
        database=config.get_bool("database") or False,
        egress=config.get_bool("egress") or False,
        connections=config.get_bool("connections") or False,
        warm=config.get_bool("warm") or False,
        build_tools_image=tools,
        build_frontend_image=frontend,
        gateway_image=image,
        gateway_keyring=keyring,
        gateway_jwks=jwks,
        org_id=org,
        timer_jwks=timer_jwks_setting(config.get("timer_jwks")),
        datagw_image=datagw_image,
        datagw_connections=datagw_connections_setting(
            config.get("datagw_connections"), datagw_image
        ),
        proxy_image=proxy_settings(config.get("proxy_image"), org),
        proxy_ha=config.get_bool("proxy_ha") or False,
        oncall_email=config.get("oncall_email"),
    )


def build_images(tools: str | None, frontend: str | None) -> tuple[str | None, str | None]:
    """Both build images or neither (the agent refuses to start on a partial set), each in the
    platform registry and pinned by digest."""
    if not tools and not frontend:
        return None, None
    for key, value in zip(BUILD_IMAGES, (tools, frontend), strict=True):
        if not value or not PINNED_IMAGE.fullmatch(value):
            raise ValueError(
                f"{key} must be {n.platform_registry()}/<image>@sha256:<digest> when "
                f"{' or '.join(BUILD_IMAGES)} is set"
            )
    return tools, frontend


def gateway_settings(
    image: str | None, keyring: str | None, jwks: str | None, org: str | None
) -> tuple[str | None, str | None, str | None, str | None]:
    """All four gateway settings or none (``infra/README.md``): the image pinned in the platform
    registry, the keyring as base64 KMS ciphertext, its public JWKS and the customer's org."""
    if not (image or keyring or jwks or org):
        return None, None, None, None
    if not (image and keyring and jwks and org):
        raise ValueError(f"set all of {', '.join(GATEWAY_SETTINGS)} or none")
    if not PINNED_IMAGE.fullmatch(image):
        raise ValueError(f"gateway_image must be {n.platform_registry()}/<image>@sha256:<digest>")
    if not CUSTOMER_ORG.fullmatch(org):
        raise ValueError("org_id must be org_ followed by 20 lowercase letters or digits")
    try:
        sealed = base64.b64decode(keyring, validate=True)
    except binascii.Error:
        sealed = b""
    if not sealed or sealed.lstrip().startswith(b"{"):
        raise ValueError("gateway_keyring must be the keyring's KMS ciphertext, in base64")
    _check_public_jwks(jwks)
    return image, keyring, jwks, org


def datagw_settings(image: str | None, org: str | None) -> str | None:
    """``datagw_image`` pinned in the platform registry, and only with the gateway settings: the
    data gateway reads the same customer and identity JWKS (SSC-050)."""
    if not image:
        return None
    if not PINNED_IMAGE.fullmatch(image):
        raise ValueError(f"datagw_image must be {n.platform_registry()}/<image>@sha256:<digest>")
    if not org:
        raise ValueError(f"datagw_image needs the gateway settings: {', '.join(GATEWAY_SETTINGS)}")
    return image


def proxy_settings(image: str | None, org: str | None) -> str | None:
    """``proxy_image`` pinned in the platform registry, and only with the gateway settings: the
    proxy reads the customer's snapshot (SSC-053)."""
    if not image:
        return None
    if not PINNED_IMAGE.fullmatch(image):
        raise ValueError(f"proxy_image must be {n.platform_registry()}/<image>@sha256:<digest>")
    if not org:
        raise ValueError(f"proxy_image needs the gateway settings: {', '.join(GATEWAY_SETTINGS)}")
    return image


def proxy_cloud_config(image: str, org_id: str, bucket: str) -> str:
    """The proxy machine's ``user-data``: a systemd unit that runs ``image`` read-only on the
    host network and restarts it whenever it exits, after Docker is configured to pull from the
    platform registry with the machine's own identity, the host refuses tunnels to the
    metadata server and loopback (``PROXY_HOST_RANGES``), and the host firewall, which drops
    inbound TCP by default on Container-Optimized OS, admits the proxy port."""
    registry = n.platform_registry().split("/", 1)[0]
    rules = [
        f"OUTPUT -p tcp --dport {TUNNEL_PORT} -d {cidr} -j REJECT" for cidr in PROXY_HOST_RANGES
    ]
    host = [
        f"ExecStartPre=/bin/sh -c 'iptables -w -C {rule} 2>/dev/null || iptables -w -A {rule}'"
        for rule in rules
    ]
    accept = f"INPUT -p tcp --dport {PROXY_PORT} -j ACCEPT"
    host.append(
        f"ExecStartPre=/bin/sh -c 'iptables -w -C {accept} 2>/dev/null || iptables -w -I {accept}'"
    )
    run = (
        "/usr/bin/docker run --rm --name ssc-egress --network host --read-only --tmpfs /tmp "
        "--cap-drop ALL --security-opt no-new-privileges "
        f"-e SSC_ORG_ID={org_id} -e SSC_CELL_BUCKET={bucket} {image}"
    )
    unit = "\n".join(
        (
            "[Unit]",
            "Description=SSC egress proxy",
            "Wants=network-online.target",
            "After=network-online.target",
            "StartLimitIntervalSec=0",
            "[Service]",
            "Environment=HOME=/var/lib/ssc-egress",
            "ExecStartPre=/bin/mkdir -p /var/lib/ssc-egress",
            f"ExecStartPre=/usr/bin/docker-credential-gcr configure-docker --registries {registry}",
            *host,
            "ExecStartPre=-/usr/bin/docker rm -f ssc-egress",
            f"ExecStart={run}",
            f"ExecStop=/usr/bin/docker stop -t {PROXY_STOP_SECONDS} ssc-egress",
            "Restart=always",
            "RestartSec=2",
            "[Install]",
            "WantedBy=multi-user.target",
        )
    )
    content = "\n".join(f"      {line}" for line in unit.split("\n"))
    return (
        "#cloud-config\n"
        "write_files:\n"
        f"  - path: /etc/systemd/system/{PROXY_UNIT}\n"
        "    permissions: '0644'\n"
        "    owner: root\n"
        "    content: |\n"
        f"{content}\n"
        "runcmd:\n"
        "  - systemctl daemon-reload\n"
        f"  - systemctl start {PROXY_UNIT}\n"
    )


def datagw_connections_setting(value: str | None, image: str | None) -> str | None:
    """``datagw_connections``: each connection the data gateway mounts, as ``con_<20>:<version>``
    separated by commas, every version a number (never ``latest``), and only with
    ``datagw_image``. Returned sorted, so the same connections are always the same setting."""
    if not value:
        return None
    if not image:
        raise ValueError("datagw_connections needs datagw_image")
    pinned: dict[str, str] = {}
    for item in value.split(","):
        connection, _, version = item.strip().partition(":")
        if CONNECTION_ID.fullmatch(connection) is None or SECRET_VERSION.fullmatch(version) is None:
            raise ValueError(
                "datagw_connections is con_<20>:<version>, separated by commas, each version a "
                "number"
            )
        if connection in pinned:
            raise ValueError(f"datagw_connections names {connection} twice")
        pinned[connection] = version
    return ",".join(f"{c}:{v}" for c, v in sorted(pinned.items()))


def connection_versions(setting: str | None) -> dict[str, str]:
    """``datagw_connections`` as a connection id to its pinned version."""
    if not setting:
        return {}
    return dict(item.split(":", 1) for item in setting.split(","))


def _check_public_jwks(jwks: str) -> None:
    """A JWKS of named keys with no private member, as ``python -m ssc_edge.keys jwks`` prints."""
    if public_jwks_kids(jwks) is None:
        raise ValueError("gateway_jwks must be the public JWKS of the gateway keyring")


def timer_jwks_setting(jwks: str | None) -> str | None:
    """One or two named public keys, as ``python -m ssc_control.timers.https jwks`` prints; the
    same for every cell, since one control plane signs every timer call."""
    if not jwks:
        return None
    kids = public_jwks_kids(jwks)
    if kids is None or len(kids) > MAX_TIMER_KEYS:
        raise ValueError("timer_jwks must be the control plane's public timer JWKS, 1 or 2 keys")
    return jwks


def _int_or(value: int | None, default: int) -> int:
    """``value`` when set, 0 included."""
    return default if value is None else value


def secret_condition() -> gcp.projects.IAMMemberConditionArgs:
    """App secrets (``ssc-a-*``) and connection secrets (``ssc-conn-*``, SSC-051)."""
    return gcp.projects.IAMMemberConditionArgs(
        title=f"only {' and '.join(f'{p}*' for p in SECRET_PREFIXES)} secrets",
        expression=" || ".join(
            APP_CONDITION.format(kind="secrets", prefix=p) for p in SECRET_PREFIXES
        ),
    )


def files_condition(bucket: str) -> gcp.storage.BucketIAMMemberConditionArgs:
    """Objects under ``files/``, and a listing whose prefix is under it: a list is checked on
    the bucket, so its prefix is the only thing to limit (SSC-046)."""
    objects = f"projects/_/buckets/{bucket}/objects/{FILES_PREFIX}"
    return gcp.storage.BucketIAMMemberConditionArgs(
        title="only app files",
        expression=f'resource.name.startsWith("{objects}") || '
        f"api.getAttribute('storage.googleapis.com/objectListPrefix', '')"
        f".startsWith('{FILES_PREFIX}')",
    )


def control_for(accounts: dict[str, str], stage: n.Stage, public: str | None = None) -> str:
    """The control account a cell trusts: the public stage's, which serves the cell's login and
    keys (SSC-064), else the cell's own stage's."""
    trusted = public or stage
    if trusted not in accounts:
        raise ValueError(f"the platform stack has no {trusted} control plane yet")
    return accounts[trusted]


class Cell:
    """Builds the resources in dependency order; each step keeps what later steps need.

    Onboarding builds everything but the lazy resources, which only their flags add
    (``naming.LAZY_RESOURCES``). The public entry (SSC-088) fronts ``gateway_`` and, each on its
    reserved host only, ``agent_`` (SSC-095) and ``intake_`` (SSC-026)."""

    def __init__(self, cfg: CellConfig, platform: pulumi.StackReference) -> None:
        self.cfg = cfg
        self.platform = platform
        self.opts = pulumi.ResourceOptions(provider=provider())
        self.apis: list[gcp.projects.Service] = []
        self.oncall_channel: gcp.monitoring.NotificationChannel | None = None

    def _kept(self) -> pulumi.ResourceOptions:
        """Cloud Run holds addresses in a subnet for up to 2 h after a service goes; the project's
        deletion takes the network instead."""
        return pulumi.ResourceOptions.merge(
            self._o(), pulumi.ResourceOptions(retain_on_delete=True)
        )

    def _o(self, *depends_on: pulumi.Resource) -> pulumi.ResourceOptions:
        """Everything in the cell waits for every API, which Google enables asynchronously."""
        return pulumi.ResourceOptions.merge(
            self.opts, pulumi.ResourceOptions(depends_on=[*self.apis, *depends_on])
        )

    def build(self) -> None:
        cfg = self.cfg
        self.project()
        self.oncall()
        self.budget()
        self.identities()
        self.keys()
        self.network()
        self.proxy_check()
        if cfg.database:
            self.database()
        self.registry()
        self.dns_policy()
        self.bucket()
        self.log_views()
        self.gateway()
        self.cell_agent()
        self.secret_intake()
        self.entry()
        if cfg.egress:
            self.proxy()
        if cfg.connections:
            self.data_gateway()
        self.deny()
        self.alerts()
        if cfg.probe:
            self.probe()
            self.probe_runner()
        self.exports()

    def project(self) -> None:
        cfg = self.cfg
        folder = self.platform.require_output("stage_folder_ids").apply(lambda f: f[cfg.stage])
        self.project_ = gcp.organizations.Project(
            "project",
            project_id=cfg.project_id,
            name=cfg.project_id,
            folder_id=folder,
            billing_account=cfg.billing_account,
            auto_create_network=False,
            deletion_policy="DELETE" if cfg.disposable else "PREVENT",
            labels={n.CELL_LABEL_KEY: cfg.label, "ssc-stage": cfg.stage},
            opts=self.opts,
        )
        self.pid = self.project_.project_id
        self.apis = [
            gcp.projects.Service(
                api.split(".")[0],
                project=self.pid,
                service=api,
                disable_on_destroy=False,
                opts=self._o(),
            )
            for api in APIS
        ]
        public = self.platform.get_output("control_public_stage")
        self.control_sa = pulumi.Output.all(
            self.platform.require_output("control_service_accounts"), public
        ).apply(lambda a: control_for(a[0], cfg.stage, a[1]))
        self.control_worker = pulumi.Output.all(
            self.platform.require_output("control_workers"), public
        ).apply(lambda a: control_for(a[0], cfg.stage, a[1]))

    def oncall(self) -> None:
        """The cell's email channel (SSC-062), made only when ``oncall_email`` is set."""
        email = self.cfg.oncall_email
        self.oncall_channel = (
            alerts.notification_channel(self.pid, email, self._o()) if email else None
        )

    def alerts(self) -> None:
        """The cell's on-call alerts (SSC-062). A cell with nothing running sends none: every
        alert counts log lines or load balancer errors, and none keys on missing data."""
        if self.oncall_channel is not None:
            alerts.cell_alerts(self.pid, self.oncall_channel, self._o())

    def budget(self) -> None:
        """An alert on the cell project itself, sized to a full cell (A7). With an on-call
        channel the budget notifies it too (SSC-062)."""
        channel = self.oncall_channel
        gcp.billing.Budget(
            "cell-monthly",
            billing_account=self.cfg.billing_account,
            display_name=f"SSC cell {self.cfg.label} ${CELL_BUDGET_USD} a month",
            amount=gcp.billing.BudgetAmountArgs(
                specified_amount=gcp.billing.BudgetAmountSpecifiedAmountArgs(
                    currency_code="USD", units=str(CELL_BUDGET_USD)
                )
            ),
            budget_filter=gcp.billing.BudgetBudgetFilterArgs(
                calendar_period="MONTH",
                projects=[pulumi.Output.concat("projects/", self.project_.number)],
            ),
            threshold_rules=[
                gcp.billing.BudgetThresholdRuleArgs(threshold_percent=p, spend_basis=b)
                for p, b in BUDGET_THRESHOLDS
            ],
            all_updates_rule=(
                gcp.billing.BudgetAllUpdatesRuleArgs(
                    monitoring_notification_channels=[channel.name]
                )
                if channel
                else None
            ),
            opts=self._o(),
        )

    def _sa(self, account: str, display: str) -> gcp.serviceaccount.Account:
        return gcp.serviceaccount.Account(
            account,
            project=self.pid,
            account_id=account,
            display_name=display,
            opts=self._o(),
        )

    def _project_role(
        self,
        name: str,
        member: pulumi.Input[str],
        role: str,
        condition: gcp.projects.IAMMemberConditionArgs | None = None,
    ) -> None:
        gcp.projects.IAMMember(
            name,
            project=self.pid,
            member=member,
            role=role,
            condition=condition,
            opts=self._o(),
        )

    def identities(self) -> None:
        """Every grant is made here at onboarding, whatever the flags: the cell deployer that
        turns flags on holds no IAM role (SSC-087). The control plane holds none in the cell; the
        secret intake adds versions to app and connection secrets and does nothing else
        (SSC-026)."""
        self.gateway_sa = self._sa("ssc-gateway", "SSC cell gateway")
        self.agent_sa = self._sa("ssc-cell-agent", "SSC cell agent")
        self.build_sa = self._sa("ssc-build", "SSC builds")
        self.data_sa = self._sa("ssc-data", "SSC data gateway and file broker")
        self.intake_sa = self._sa(n.SECRET_INTAKE, "SSC secret intake")
        self.proxy_sa = self._sa(PROXY_SA, "SSC egress proxy")
        agent = self.agent_sa.member
        create_role = gcp.projects.IAMCustomRole(
            "agent-create",
            project=self.pid,
            role_id="sscCellAgentCreate",
            title="SSC cell agent: create app resources",
            description="Create calls cannot be limited by name in IAM; the agent's code does it.",
            permissions=list(CREATE_PERMISSIONS),
            opts=self._o(),
        )
        runtime_role = gcp.projects.IAMCustomRole(
            "agent-runtime",
            project=self.pid,
            role_id="sscCellAgentRuntime",
            title="SSC cell agent: run app services",
            description="Cloud Run and service accounts take no name conditions; the agent's code "
            "limits these to ssc-a- names.",
            permissions=list(RUNTIME_PERMISSIONS),
            opts=self._o(),
        )
        database_role = gcp.projects.IAMCustomRole(
            "agent-database",
            project=self.pid,
            role_id="sscCellAgentDatabase",
            title="SSC cell agent: app databases",
            description="What the agent calls on the cell's Cloud SQL instance (SSC-040, SSC-042).",
            permissions=list(DATABASE_PERMISSIONS),
            opts=self._o(),
        )
        usage_role = gcp.projects.IAMCustomRole(
            "agent-usage",
            project=self.pid,
            role_id="sscCellAgentUsage",
            title="SSC cell agent: app usage",
            description="Reads Cloud Run's metrics from the cell's Cloud Monitoring (SSC-028).",
            permissions=list(USAGE_PERMISSIONS),
            opts=self._o(),
        )
        for name, role in (
            ("agent-create", create_role),
            ("agent-runtime", runtime_role),
            ("agent-database", database_role),
            ("agent-usage", usage_role),
        ):
            gcp.projects.IAMMember(
                name, project=self.pid, member=agent, role=role.name, opts=self._o()
            )
        self._project_role("agent-secrets", agent, "roles/secretmanager.admin", secret_condition())
        self._project_role(
            "intake-secret-versions",
            self.intake_sa.member,
            "roles/secretmanager.secretVersionAdder",
            secret_condition(),
        )
        self.connection_tag = self._connection_tag()
        self._project_role("build-logs", self.build_sa.member, "roles/logging.logWriter")
        self._project_role("gateway-logs", self.gateway_sa.member, "roles/logging.logWriter")
        self._project_role("agent-logs", agent, "roles/logging.logWriter")
        self._project_role("data-logs", self.data_sa.member, "roles/logging.logWriter")
        self._project_role("intake-logs", self.intake_sa.member, "roles/logging.logWriter")
        self._project_role("proxy-logs", self.proxy_sa.member, "roles/logging.logWriter")
        self.files_role = gcp.projects.IAMCustomRole(
            "agent-files",
            project=self.pid,
            role_id="sscCellAgentFiles",
            title="SSC cell agent: drop app files",
            description="Deletes an environment's files in the cell bucket, under files/ alone "
            "(SSC-046).",
            permissions=list(FILES_PERMISSIONS),
            opts=self._o(),
        )
        gcp.serviceaccount.IAMMember(
            "data-signs-as-itself",
            service_account_id=self.data_sa.name,
            role="roles/iam.serviceAccountTokenCreator",
            member=self.data_sa.member,
            opts=self._o(),
        )

    def _connection_tag(self) -> tuple[pulumi.Output[str], pulumi.Output[str]]:
        """The project's own tag ``ssc-secret-kind=connection`` (SSC-051), so no organisation tag
        permission is needed. The agent binds it to each connection secret as it creates one,
        and the deny rule lets ``ssc-data`` read only secrets that carry it. Only the agent is
        granted to bind it here. Returns the tag key and value IDs."""
        key = gcp.tags.TagKey(
            "secret-kind-key",
            parent=pulumi.Output.concat("projects/", self.pid),
            short_name=SECRET_KIND_KEY,
            description="What a cell secret holds; only connection secrets are tagged.",
            opts=self._o(),
        )
        key_id = pulumi.Output.concat("tagKeys/", key.name)
        value = gcp.tags.TagValue(
            "secret-kind-connection",
            parent=key_id,
            short_name=CONNECTION_KIND,
            description="A customer connection's credentials, read by ssc-data alone (SSC-051).",
            opts=self._o(),
        )
        value_id = pulumi.Output.concat("tagValues/", value.name)
        gcp.tags.TagValueIamMember(
            "secret-kind-connection-agent",
            tag_value=value_id,
            role=TAG_USER,
            member=self.agent_sa.member,
            opts=self._o(),
        )
        return key_id, value_id

    def keys(self) -> None:
        ring = gcp.kms.KeyRing(
            "keyring",
            project=self.pid,
            name="ssc-cell",
            location=n.REGION,
            opts=self._o(),
        )
        self.sql_key = gcp.kms.CryptoKey(
            "key-sql", key_ring=ring.id, name="sql", rotation_period=KEY_ROTATION, opts=self._o()
        )
        self.registry_key = gcp.kms.CryptoKey(
            "key-registry",
            key_ring=ring.id,
            name="registry",
            rotation_period=KEY_ROTATION,
            opts=self._o(),
        )
        self.gateway_key = gcp.kms.CryptoKey(
            "key-gateway",
            key_ring=ring.id,
            name="gateway",
            rotation_period=KEY_ROTATION,
            opts=self._o(),
        )
        self.gateway_key_grant = gcp.kms.CryptoKeyIAMMember(
            "gateway-key",
            crypto_key_id=self.gateway_key.id,
            role="roles/cloudkms.cryptoKeyDecrypter",
            member=self.gateway_sa.member,
            opts=self._o(),
        )
        gcp.kms.CryptoKeyIAMMember(
            "operator-gateway-key",
            crypto_key_id=self.gateway_key.id,
            role="roles/cloudkms.cryptoKeyEncrypter",
            member=n.OPERATOR,
            opts=self._o(),
        )
        self.bucket_key = gcp.kms.CryptoKey(
            "key-bucket",
            key_ring=ring.id,
            name="bucket",
            rotation_period=KEY_ROTATION,
            opts=self._o(),
        )
        storage_agent = gcp.storage.get_project_service_account_output(
            project=self.pid, opts=pulumi.InvokeOutputOptions(depends_on=self.apis)
        )
        self.bucket_key_grant = gcp.kms.CryptoKeyIAMMember(
            "storage-agent-key",
            crypto_key_id=self.bucket_key.id,
            role="roles/cloudkms.cryptoKeyEncrypterDecrypter",
            member=pulumi.Output.concat("serviceAccount:", storage_agent.email_address),
            opts=self._o(),
        )
        self.key_grants: list[pulumi.Resource] = []
        for name, service, key in (
            ("sql", "sqladmin.googleapis.com", self.sql_key),
            ("registry", "artifactregistry.googleapis.com", self.registry_key),
        ):
            agent = gcp.projects.ServiceIdentity(
                f"{name}-agent", project=self.pid, service=service, opts=self._o()
            )
            self.key_grants.append(
                gcp.kms.CryptoKeyIAMMember(
                    f"{name}-agent-key",
                    crypto_key_id=key.id,
                    role="roles/cloudkms.cryptoKeyEncrypterDecrypter",
                    member=agent.member,
                    opts=self._o(),
                )
            )

    def network(self) -> None:
        self.vpc = gcp.compute.Network(
            "vpc",
            project=self.pid,
            name="ssc-cell",
            auto_create_subnetworks=False,
            routing_mode="REGIONAL",
            opts=self._kept(),
        )
        subnets = {name: self._subnet(name, cidr) for name, cidr in SUBNETS.items()}
        self.apps_subnet = subnets[APPS_SUBNET]
        self.edge_subnet = subnets[EDGE_SUBNET]
        self.proxy_ip = self._reserve("proxy-ip", "ssc-proxy", PROXY_HOST)
        self.datagw_ip = self._reserve("datagw-ip", "ssc-datagw", DATAGW_HOST)
        psa = gcp.compute.GlobalAddress(
            "psa-range",
            project=self.pid,
            name="ssc-psa",
            purpose="VPC_PEERING",
            address_type="INTERNAL",
            address=PSA_ADDRESS,
            prefix_length=PSA_PREFIX,
            network=self.vpc.id,
            opts=self._o(),
        )
        self.psa = gcp.servicenetworking.Connection(
            "psa",
            network=self.vpc.id,
            service="servicenetworking.googleapis.com",
            reserved_peering_ranges=[psa.name],
            deletion_policy="ABANDON",  # Google holds it for a while after Cloud SQL is deleted
            opts=self._o(),
        )
        self._egress("egress-deny-all", 65534, deny=True, ranges=["0.0.0.0/0"])
        self._egress("egress-internal", 1000, ranges=internal_ranges())
        self._egress("egress-google-private", 1000, ranges=[GOOGLE_PRIVATE_RANGE])
        for name, tag in (("gateway", GATEWAY_TAG), ("proxy", PROXY_TAG), ("data", DATA_TAG)):
            self._egress(f"egress-{name}", 1000, ranges=["0.0.0.0/0"], tags=[tag])
        self._egress(
            "egress-proxy-private", 900, deny=True, ranges=PRIVATE_RANGES, tags=[PROXY_TAG]
        )
        self._ingress("ingress-proxy", [SUBNETS[APPS_SUBNET]], PROXY_TAG, PROXY_PORT)
        self._ingress("ingress-proxy-health", HEALTH_CHECK_RANGES, PROXY_TAG, PROXY_PORT)
        self._private_google_dns()
        self._database_dns()
        router = gcp.compute.Router(
            "router",
            project=self.pid,
            name="ssc-cell",
            region=n.REGION,
            network=self.vpc.id,
            opts=self._o(),
        )
        self.nat_ip = gcp.compute.Address(
            "nat-ip-gateway",
            project=self.pid,
            name="nat-gateway",
            region=n.REGION,
            address_type="EXTERNAL",
            network_tier="PREMIUM",
            opts=self._o(),
        )
        gcp.compute.RouterNat(
            "nat-gateway",
            project=self.pid,
            name="nat-gateway",
            region=n.REGION,
            router=router.name,
            nat_ip_allocate_option="MANUAL_ONLY",
            nat_ips=[self.nat_ip.self_link],
            source_subnetwork_ip_ranges_to_nat="LIST_OF_SUBNETWORKS",
            subnetworks=[
                gcp.compute.RouterNatSubnetworkArgs(
                    name=self.edge_subnet.id, source_ip_ranges_to_nats=["ALL_IP_RANGES"]
                )
            ],
            opts=self._o(),
        )

    def _reserve(self, resource: str, name: str, host: int) -> gcp.compute.Address:
        """A fixed internal address in the edge subnet, held before anything uses it."""
        return gcp.compute.Address(
            resource,
            project=self.pid,
            name=name,
            region=n.REGION,
            address_type="INTERNAL",
            subnetwork=self.edge_subnet.id,
            address=edge_address(host),
            opts=self._o(),
        )

    def _subnet(self, name: str, cidr: str) -> gcp.compute.Subnetwork:
        return gcp.compute.Subnetwork(
            f"subnet-{name}",
            project=self.pid,
            name=name,
            region=n.REGION,
            network=self.vpc.id,
            ip_cidr_range=cidr,
            stack_type="IPV4_ONLY",
            private_ip_google_access=True,
            opts=self._kept(),
        )

    def _egress(
        self,
        name: str,
        priority: int,
        *,
        ranges: Sequence[str],
        deny: bool = False,
        tags: Sequence[str] | None = None,
    ) -> None:
        gcp.compute.Firewall(
            name,
            project=self.pid,
            name=name,
            network=self.vpc.id,
            direction="EGRESS",
            priority=priority,
            destination_ranges=list(ranges),
            denies=[gcp.compute.FirewallDenyArgs(protocol="all")] if deny else None,
            allows=None if deny else [gcp.compute.FirewallAllowArgs(protocol="all")],
            target_tags=list(tags) if tags else None,
            opts=self._o(),
        )

    def _ingress(self, name: str, sources: Sequence[str], tag: str, port: int) -> None:
        gcp.compute.Firewall(
            name,
            project=self.pid,
            name=name,
            network=self.vpc.id,
            direction="INGRESS",
            priority=1000,
            source_ranges=list(sources),
            target_tags=[tag],
            allows=[gcp.compute.FirewallAllowArgs(protocol="tcp", ports=[str(port)])],
            opts=self._o(),
        )

    def _private_google_dns(self) -> None:
        zone = gcp.dns.ManagedZone(
            "googleapis",
            project=self.pid,
            name="googleapis",
            dns_name="googleapis.com.",
            visibility="private",
            private_visibility_config=gcp.dns.ManagedZonePrivateVisibilityConfigArgs(
                networks=[
                    gcp.dns.ManagedZonePrivateVisibilityConfigNetworkArgs(network_url=self.vpc.id)
                ]
            ),
            opts=self._o(),
        )
        gcp.dns.RecordSet(
            "googleapis-a",
            project=self.pid,
            managed_zone=zone.name,
            name="private.googleapis.com.",
            type="A",
            ttl=300,
            rrdatas=list(GOOGLE_PRIVATE),
            opts=self._o(),
        )
        gcp.dns.RecordSet(
            "googleapis-cname",
            project=self.pid,
            managed_zone=zone.name,
            name="*.googleapis.com.",
            type="CNAME",
            ttl=300,
            rrdatas=["private.googleapis.com."],
            opts=self._o(),
        )

    def _database_dns(self) -> None:
        """The zone the database's name is answered from (SSC-040), empty until the ``database``
        flag adds its record. The cell deployer that adds it may write records in this zone
        alone, through a role holding only record permissions."""
        self.sql_zone = gcp.dns.ManagedZone(
            "sql-zone",
            project=self.pid,
            name="ssc-sql",
            dns_name=SQL_DNS_ZONE,
            visibility="private",
            private_visibility_config=gcp.dns.ManagedZonePrivateVisibilityConfigArgs(
                networks=[
                    gcp.dns.ManagedZonePrivateVisibilityConfigNetworkArgs(network_url=self.vpc.id)
                ]
            ),
            opts=self._o(),
        )
        records = gcp.projects.IAMCustomRole(
            "deployer-sql-records",
            project=self.pid,
            role_id="sscDeployerRecords",
            title="SSC cell deployer: database DNS records",
            description="Records in the ssc-sql zone, where it is granted, and nothing else.",
            permissions=list(ZONE_RECORD_PERMISSIONS),
            opts=self._o(),
        )
        deployer = self.platform.require_output("cell_deployer").apply(
            lambda d: f"serviceAccount:{d['service_account']}"
        )
        gcp.dns.DnsManagedZoneIamMember(
            "deployer-sql-records",
            project=self.pid,
            managed_zone=self.sql_zone.name,
            role=records.name,
            member=deployer,
            opts=self._o(),
        )

    def database(self) -> None:
        """The ``database`` flag: one instance for every app database in the cell, reached by
        apps on its private address under its DNS name, which its CA-issued certificate names
        (``sslmode=verify-full``), and by the agent through the Data API only (SSC-040)."""
        cfg = self.cfg
        self.sql = gcp.sql.DatabaseInstance(
            "sql",
            project=self.pid,
            name=SQL_INSTANCE,
            region=n.REGION,
            database_version="POSTGRES_18",
            encryption_key_name=self.sql_key.id,
            deletion_protection=not cfg.disposable,
            settings=gcp.sql.DatabaseInstanceSettingsArgs(
                tier=SQL_TIER,
                edition="ENTERPRISE",
                availability_type="ZONAL",
                deletion_protection_enabled=not cfg.disposable,
                data_api_access="ALLOW_DATA_API",
                ip_configuration=gcp.sql.DatabaseInstanceSettingsIpConfigurationArgs(
                    ipv4_enabled=False,
                    private_network=self.vpc.id,
                    ssl_mode="ENCRYPTED_ONLY",
                    server_ca_mode=SQL_CA_MODE,
                ),
                backup_configuration=gcp.sql.DatabaseInstanceSettingsBackupConfigurationArgs(
                    enabled=True,
                    point_in_time_recovery_enabled=True,
                    start_time="08:00",
                    transaction_log_retention_days=7,
                ),
                database_flags=[
                    gcp.sql.DatabaseInstanceSettingsDatabaseFlagArgs(
                        name="cloudsql.iam_authentication", value="on"
                    ),
                    gcp.sql.DatabaseInstanceSettingsDatabaseFlagArgs(
                        name="max_connections", value=SQL_MAX_CONNECTIONS
                    ),
                ],
                user_labels={n.CELL_LABEL_KEY: cfg.label},
            ),
            opts=self._o(self.psa, *self.key_grants),
        )
        gcp.sql.User(
            "sql-agent",
            project=self.pid,
            instance=self.sql.name,
            name=self.agent_sa.email.apply(lambda e: e.removesuffix(".gserviceaccount.com")),
            type="CLOUD_IAM_SERVICE_ACCOUNT",
            database_roles=["cloudsqlsuperuser"],
            opts=self._o(),
        )
        gcp.dns.RecordSet(
            "sql-dns",
            project=self.pid,
            managed_zone=self.sql_zone.name,
            name=pulumi.Output.all(self.sql.dns_names, self.sql.dns_name).apply(
                lambda a: sql_dns_name(a[0], a[1])
            ),
            type="A",
            ttl=DNS_TTL,
            rrdatas=[self.sql.private_ip_address],
            opts=self._o(),
        )

    def registry(self) -> None:
        self.repo = gcp.artifactregistry.Repository(
            "registry",
            project=self.pid,
            location=n.REGION,
            repository_id="ssc-apps",
            format="DOCKER",
            kms_key_name=self.registry_key.id,
            opts=self._o(*self.key_grants),
        )
        self.app_images = pulumi.Output.concat(
            n.REGION, "-docker.pkg.dev/", self.pid, "/", self.repo.repository_id, "/", APP_IMAGE
        )
        self.platform_repo = gcp.artifactregistry.Repository(
            "registry-platform",
            project=self.pid,
            location=n.REGION,
            repository_id="ssc-platform",
            format="DOCKER",
            description="SSC's own images in this cell: the cell agent.",
            kms_key_name=self.registry_key.id,
            opts=self._o(*self.key_grants),
        )
        gcp.artifactregistry.RepositoryIamMember(
            "registry-build",
            project=self.pid,
            location=n.REGION,
            repository=self.repo.name,
            role="roles/artifactregistry.writer",
            member=self.build_sa.member,
            opts=self._o(),
        )
        gcp.artifactregistry.RepositoryIamMember(
            "registry-build-tools",
            project=n.BOOTSTRAP_PROJECT,
            location=n.REGION,
            repository=n.PLATFORM_REPOSITORY,
            role="roles/artifactregistry.reader",
            member=self.build_sa.member,
            opts=self._o(),
        )
        run_agent = gcp.projects.ServiceIdentity(
            "run-agent", project=self.pid, service="run.googleapis.com", opts=self._o()
        )
        gcp.artifactregistry.RepositoryIamMember(
            "registry-gateway-image",
            project=n.BOOTSTRAP_PROJECT,
            location=n.REGION,
            repository=n.PLATFORM_REPOSITORY,
            role="roles/artifactregistry.reader",
            member=run_agent.member,
            opts=self._o(),
        )
        gcp.artifactregistry.RepositoryIamMember(
            "registry-proxy-image",
            project=n.BOOTSTRAP_PROJECT,
            location=n.REGION,
            repository=n.PLATFORM_REPOSITORY,
            role="roles/artifactregistry.reader",
            member=self.proxy_sa.member,
            opts=self._o(),
        )
        # Cloud Run checks that whoever deploys an image may read it.
        gcp.artifactregistry.RepositoryIamMember(
            "registry-agent",
            project=self.pid,
            location=n.REGION,
            repository=self.repo.name,
            role="roles/artifactregistry.reader",
            member=self.agent_sa.member,
            opts=self._o(),
        )

    def dns_policy(self) -> None:
        """Names resolve inside the cell only when Google serves them; every other name gets an
        unroutable answer from Cloud DNS itself, so no query leaves to a public resolver (the
        ``no_dns_exfil`` probe). The egress gateway resolves allowed hosts itself.

        Measured in a probe cell: Cloud DNS ignores a ``*.`` rule, and a rule answers only the
        record types it holds, passing others (AAAA, TXT) to public DNS. So each top-level domain
        gets a ``*.<tld>.`` rule answering with a CNAME, which covers every type, to a name that
        only this policy answers. Google's names bypass it by the longer match, and so do the
        platform hosts the gateway calls (``GATEWAY_PLATFORM_HOSTS``), each by its exact name.
        Cloud SQL's names (``*.sql-psa.goog.``) bypass it to the cell's own ``ssc-sql`` zone, which
        answers them all, so none leaves the cell either.

        The policy holds for the whole VPC, so an app resolves those hosts too, and nothing more:
        no allow rule covers their addresses and the apps subnet has no NAT, so the answer leads
        nowhere (SSC-027)."""
        policy = gcp.dns.ResponsePolicy(
            "dns-policy",
            project=self.pid,
            response_policy_name="ssc-cell",
            description="Only Google's names resolve in the cell.",
            networks=[gcp.dns.ResponsePolicyNetworkArgs(network_url=self.vpc.id)],
            opts=self._o(),
        )
        sink = gcp.dns.ResponsePolicyRule(
            "dns-sinkhole",
            project=self.pid,
            response_policy=policy.response_policy_name,
            rule_name="sinkhole",
            dns_name=SINKHOLE_NAME,
            local_data=gcp.dns.ResponsePolicyRuleLocalDataArgs(
                local_datas=[
                    gcp.dns.ResponsePolicyRuleLocalDataLocalDataArgs(
                        name=SINKHOLE_NAME, type=kind, ttl=300, rrdatas=[address]
                    )
                    for kind, address in (("A", SINKHOLE), ("AAAA", SINKHOLE_V6))
                ]
            ),
            opts=self._o(),
        )
        for tld in tlds():
            gcp.dns.ResponsePolicyRule(
                f"dns-tld-{tld}",
                project=self.pid,
                response_policy=policy.response_policy_name,
                rule_name=f"tld-{tld}",
                dns_name=f"*.{tld}.",
                local_data=gcp.dns.ResponsePolicyRuleLocalDataArgs(
                    local_datas=[
                        gcp.dns.ResponsePolicyRuleLocalDataLocalDataArgs(
                            name=f"*.{tld}.", type="CNAME", ttl=300, rrdatas=[SINKHOLE_NAME]
                        )
                    ]
                ),
                opts=self._o(sink),
            )
        for i, name in enumerate(GOOGLE_DNS_PASSTHRU):
            gcp.dns.ResponsePolicyRule(
                f"dns-google-{i}",
                project=self.pid,
                response_policy=policy.response_policy_name,
                rule_name=f"google-{i}",
                dns_name=name,
                behavior="bypassResponsePolicy",
                opts=self._o(),
            )
        gcp.dns.ResponsePolicyRule(
            "dns-sql",
            project=self.pid,
            response_policy=policy.response_policy_name,
            rule_name="sql",
            dns_name=SQL_DNS_NAMES,
            behavior="bypassResponsePolicy",
            opts=self._o(),
        )
        for host in n.GATEWAY_PLATFORM_HOSTS:
            label = host.split(".", 1)[0]
            gcp.dns.ResponsePolicyRule(
                f"dns-platform-{label}",
                project=self.pid,
                response_policy=policy.response_policy_name,
                rule_name=f"platform-{label}",
                dns_name=f"{host}.",
                behavior="bypassResponsePolicy",
                opts=self._o(),
            )

    def bucket(self) -> None:
        """The cell bucket: snapshots, audit anchors, source bundles and the apps' files
        (SSC-046), encrypted with the cell's own key ``bucket``. Under ``files/<env_id>/`` only
        ``ssc-data``, the file broker, reads and writes, and the agent deletes an environment's
        files when it is gone; a replaced or deleted file stays a noncurrent version for
        ``NONCURRENT_FILE_DAYS``."""
        cfg = self.cfg
        self.bucket_ = gcp.storage.Bucket(
            "bucket",
            project=self.pid,
            name=n.cell_bucket(cfg.label),
            location=n.REGION.upper(),
            uniform_bucket_level_access=True,
            public_access_prevention="enforced",
            versioning=gcp.storage.BucketVersioningArgs(enabled=True),
            encryption=gcp.storage.BucketEncryptionArgs(default_kms_key_name=self.bucket_key.id),
            lifecycle_rules=[
                gcp.storage.BucketLifecycleRuleArgs(
                    action=gcp.storage.BucketLifecycleRuleActionArgs(type="Delete"),
                    condition=gcp.storage.BucketLifecycleRuleConditionArgs(
                        matches_prefixes=[FILES_PREFIX],
                        with_state="ARCHIVED",
                        days_since_noncurrent_time=NONCURRENT_FILE_DAYS,
                    ),
                )
            ],
            force_destroy=cfg.disposable,
            opts=self._o(self.bucket_key_grant),
        )
        for name, member, role in (
            (
                "bucket-control-worker",
                pulumi.Output.concat("serviceAccount:", self.control_worker),
                "roles/storage.objectUser",
            ),
            ("bucket-gateway", self.gateway_sa.member, "roles/storage.objectViewer"),
            ("bucket-agent", self.agent_sa.member, "roles/storage.objectViewer"),
        ):
            gcp.storage.BucketIAMMember(
                name, bucket=self.bucket_.name, role=role, member=member, opts=self._o()
            )
        snapshots = f"projects/_/buckets/{n.cell_bucket(cfg.label)}/objects/snapshots/"
        for name, account in (("bucket-data", self.data_sa), ("bucket-proxy", self.proxy_sa)):
            gcp.storage.BucketIAMMember(
                name,
                bucket=self.bucket_.name,
                role="roles/storage.objectViewer",
                member=account.member,
                condition=gcp.storage.BucketIAMMemberConditionArgs(
                    title="only access snapshots",
                    expression=f'resource.name.startsWith("{snapshots}")',
                ),
                opts=self._o(),
            )
        files = files_condition(n.cell_bucket(cfg.label))
        gcp.storage.BucketIAMMember(
            "bucket-data-files",
            bucket=self.bucket_.name,
            role="roles/storage.objectUser",
            member=self.data_sa.member,
            condition=files,
            opts=self._o(),
        )
        gcp.storage.BucketIAMMember(
            "bucket-agent-files",
            bucket=self.bucket_.name,
            role=self.files_role.name,
            member=self.agent_sa.member,
            condition=files,
            opts=self._o(),
        )

    def log_views(self) -> None:
        """The agent's only window on the cell's logs (SSC-024): one view per resource type on
        ``_Default``, since a view's filter may not hold ``OR``, and ``logging.viewAccessor`` on
        those two views by name. The cell folders set the default log location to the region
        (``platform._log_location``), so ``_Default`` is made there with the project."""
        bucket = log_bucket(self.cfg.project_id)
        self.log_view_names = [f"{bucket}/views/{view}" for view in LOG_VIEWS]
        for view, log_filter in LOG_VIEWS.items():
            gcp.logging.LogView(
                f"log-view-{view}",
                name=view,
                bucket=bucket,
                location=n.REGION,
                description="What the cell agent reads for app logs and health (SSC-024).",
                filter=log_filter,
                opts=self._o(),
            )
        self._project_role(
            "agent-log-views",
            self.agent_sa.member,
            "roles/logging.viewAccessor",
            gcp.projects.IAMMemberConditionArgs(
                title="only the cell's log views",
                expression=" || ".join(f'resource.name == "{v}"' for v in self.log_view_names),
            ),
        )

    def _run(  # noqa: PLR0913  (keyword-only)
        self,
        name: str,
        sa: gcp.serviceaccount.Account,
        *,
        ingress: str,
        vpc: gcp.cloudrunv2.ServiceTemplateVpcAccessArgs | None,
        instances: tuple[int, int],
        image: str | None = None,
        command: Sequence[str] | None = None,
        env: dict[str, pulumi.Input[str]] | None = None,
        secret_env: dict[str, tuple[str, str]] | None = None,
        timeout: str | None = None,
        concurrency: int | None = None,
        audiences: Sequence[str] | None = None,
        invoker_iam: bool = True,
        after: Sequence[pulumi.Resource] = (),
    ) -> gcp.cloudrunv2.Service:
        """A request-billed service: CPU only while a request is open. ``invoker_iam=False``
        leaves the caller check to the service itself. ``secret_env`` maps a variable to a cell
        secret and its pinned version, which Cloud Run reads as the service's identity."""
        min_instances, max_instances = instances
        run = gcp.cloudrunv2
        envs = {
            k: run.ServiceTemplateContainerEnvArgs(name=k, value=v) for k, v in (env or {}).items()
        } | {
            k: run.ServiceTemplateContainerEnvArgs(
                name=k,
                value_source=run.ServiceTemplateContainerEnvValueSourceArgs(
                    secret_key_ref=run.ServiceTemplateContainerEnvValueSourceSecretKeyRefArgs(
                        secret=secret, version=version
                    )
                ),
            )
            for k, (secret, version) in (secret_env or {}).items()
        }
        return gcp.cloudrunv2.Service(
            name,
            project=self.pid,
            name=name,
            location=n.REGION,
            ingress=ingress,
            custom_audiences=list(audiences) if audiences else None,
            invoker_iam_disabled=None if invoker_iam else True,
            deletion_protection=not self.cfg.disposable,
            scaling=gcp.cloudrunv2.ServiceScalingArgs(max_instance_count=max_instances),
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=sa.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=min_instances),
                timeout=timeout,
                max_instance_request_concurrency=concurrency,
                vpc_access=vpc,
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=image or PLACEHOLDER_IMAGE,
                        commands=list(command) if command else None,
                        envs=[e for _, e in sorted(envs.items())] or None,
                        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                            cpu_idle=True,
                            limits={"cpu": "1", "memory": "512Mi"},
                        ),
                    )
                ],
            ),
            opts=self._o(*after),
        )

    def _edge_vpc(self, tag: str) -> gcp.cloudrunv2.ServiceTemplateVpcAccessArgs:
        """Direct VPC egress from the edge subnet, the one the NAT serves."""
        return gcp.cloudrunv2.ServiceTemplateVpcAccessArgs(
            egress="ALL_TRAFFIC",
            network_interfaces=[
                gcp.cloudrunv2.ServiceTemplateVpcAccessNetworkInterfaceArgs(
                    network=self.vpc.id, subnetwork=self.edge_subnet.id, tags=[tag]
                )
            ],
        )

    def gateway(self) -> None:
        """Internal and load-balancer ingress keeps the ``run.app`` host closed, so the invoker
        is ``allUsers`` and the authoriser refuses requests without a session (SSC-088). The
        folder's member policy allows ``allUsers`` only where the platform's public-invoker tag is
        bound, so the tag goes on this service and the secret intake, and on no other, before the
        grant (SSC-095).

        With the gateway settings it runs ``gateway_image``, a build of
        ``packages/ssc_edge/Dockerfile`` that decrypts its keyring with the cell's ``gateway`` key
        at start (SSC-018)."""
        self.gateway_ = self._run(
            n.GATEWAY,
            self.gateway_sa,
            ingress="INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
            vpc=self._edge_vpc(GATEWAY_TAG),
            instances=(self.cfg.gateway_floor, self.cfg.gateway_max),
            image=self.cfg.gateway_image,
            env=self._gateway_env(),
            timeout=GATEWAY_TIMEOUT,
            concurrency=GATEWAY_CONCURRENCY,
            after=[self.gateway_key_grant],
        )
        self._public("gateway", self.gateway_)

    def _public(self, resource: str, service: gcp.cloudrunv2.Service) -> None:
        """``allUsers`` may invoke ``service``, which the folder's member policy allows only once
        the platform's public-invoker tag is bound to it (SSC-095)."""
        tag = gcp.tags.LocationTagBinding(
            f"{resource}-public-tag",
            parent=pulumi.Output.concat(
                "//run.googleapis.com/projects/",
                self.project_.number,
                f"/locations/{n.REGION}/services/",
                service.name,
            ),
            tag_value=self.platform.require_output("public_invoker_tag"),
            location=n.REGION,
            opts=self._o(),
        )
        gcp.cloudrunv2.ServiceIamMember(
            f"{resource}-invoker",
            project=self.pid,
            location=n.REGION,
            name=service.name,
            role="roles/run.invoker",
            member="allUsers",
            opts=self._o(tag),
        )

    def _gateway_env(self) -> dict[str, pulumi.Input[str]] | None:
        """What ``ssc_edge.server.settings_from_env`` reads, once the gateway settings are set."""
        cfg = self.cfg
        if not (cfg.gateway_image and cfg.gateway_keyring and cfg.gateway_jwks and cfg.org_id):
            return None
        env: dict[str, pulumi.Input[str]] = {
            "SSC_CELL_LABEL": cfg.label,
            "SSC_ORG_ID": cfg.org_id,
            "SSC_PROJECT_NUMBER": self.project_.number,
            "SSC_REGION": n.REGION,
            "SSC_CELL_BUCKET": self.bucket_.name,
            "SSC_GATEWAY_KEYRING": cfg.gateway_keyring,
            "SSC_GATEWAY_KMS_KEY": self.gateway_key.id,
            "SSC_IDENTITY_JWKS": cfg.gateway_jwks,
            "SSC_APPS_DOMAIN": n.APPS_DOMAIN,
            "SSC_AUTH_URL": f"https://{n.AUTH_HOST}",
            "SSC_IDENTITY_ISSUER": n.identity_issuer(cfg.label),
        }
        if cfg.timer_jwks:
            env["SSC_TIMER_JWKS"] = cfg.timer_jwks
        return env

    def entry(self) -> None:
        """The cell's own public door: a global external Application Load Balancer on one
        address, HTTPS (TLS 1.2 or later) to the gateway through a serverless NEG, the agent's
        reserved host alone to the agent through a second one, the secret intake's alone to the
        intake through a third, and HTTP answered with a redirect on the same address. A
        serverless NEG's backend timeout is fixed at 60 minutes and cannot be set, so the
        gateway's 3600 s request timeout is what bounds a WebSocket."""
        label = self.cfg.label
        self.entry_ip = gcp.compute.GlobalAddress(
            "entry-ip",
            project=self.pid,
            name=ENTRY,
            address_type="EXTERNAL",
            ip_version="IPV4",
            opts=self._o(),
        )
        gateway = self._backend("gateway", n.GATEWAY, self.gateway_)
        agent = self._backend("agent", n.CELL_AGENT, self.agent_)
        intake = self._backend("intake", n.SECRET_INTAKE, self.intake_)
        self.url_map = gcp.compute.URLMap(
            "entry-map",
            project=self.pid,
            name=ENTRY,
            default_service=gateway.id,
            host_rules=[
                gcp.compute.URLMapHostRuleArgs(
                    hosts=[n.agent_host(label)], path_matcher=AGENT_PATHS
                ),
                gcp.compute.URLMapHostRuleArgs(
                    hosts=[n.intake_host(label)], path_matcher=INTAKE_PATHS
                ),
            ],
            path_matchers=[
                gcp.compute.URLMapPathMatcherArgs(name=AGENT_PATHS, default_service=agent.id),
                gcp.compute.URLMapPathMatcherArgs(name=INTAKE_PATHS, default_service=intake.id),
            ],
            opts=self._o(),
        )
        self.certificate = self._certificate()
        tls = gcp.compute.SSLPolicy(
            "entry-tls",
            project=self.pid,
            name=ENTRY,
            min_tls_version=TLS_MIN,
            profile=TLS_PROFILE,
            opts=self._o(),
        )
        https = gcp.compute.TargetHttpsProxy(
            "entry-https",
            project=self.pid,
            name=ENTRY,
            url_map=self.url_map.id,
            ssl_policy=tls.id,
            certificate_map=pulumi.Output.concat(
                "//certificatemanager.googleapis.com/", self.certificate_map.id
            ),
            opts=self._o(),
        )
        redirect = gcp.compute.URLMap(
            "entry-redirect",
            project=self.pid,
            name=f"{ENTRY}-redirect",
            default_url_redirect=gcp.compute.URLMapDefaultUrlRedirectArgs(
                https_redirect=True,
                strip_query=False,
                redirect_response_code="MOVED_PERMANENTLY_DEFAULT",
            ),
            opts=self._o(),
        )
        http = gcp.compute.TargetHttpProxy(
            "entry-http",
            project=self.pid,
            name=f"{ENTRY}-redirect",
            url_map=redirect.id,
            opts=self._o(),
        )
        for scheme, target, port in (("https", https.id, "443"), ("http", http.id, "80")):
            gcp.compute.GlobalForwardingRule(
                f"entry-{scheme}",
                project=self.pid,
                name=f"{ENTRY}-{scheme}",
                target=target,
                ip_address=self.entry_ip.address,
                ip_protocol="TCP",
                port_range=port,
                load_balancing_scheme=LB_SCHEME,
                opts=self._o(),
            )
        self._record("dns-wildcard", f"{n.cell_wildcard(label)}.", "A", self.entry_ip.address)

    def _backend(
        self, resource: str, name: str, service: gcp.cloudrunv2.Service
    ) -> gcp.compute.BackendService:
        """A serverless NEG on one Cloud Run service and the backend service in front of it."""
        neg = gcp.compute.RegionNetworkEndpointGroup(
            f"{resource}-neg",
            project=self.pid,
            name=name,
            region=n.REGION,
            network_endpoint_type="SERVERLESS",
            cloud_run=gcp.compute.RegionNetworkEndpointGroupCloudRunArgs(service=service.name),
            opts=self._o(),
        )
        return gcp.compute.BackendService(
            f"{resource}-backend",
            project=self.pid,
            name=name,
            load_balancing_scheme=LB_SCHEME,
            protocol="HTTPS",
            backends=[gcp.compute.BackendServiceBackendArgs(group=neg.id)],
            opts=self._o(),
        )

    def _certificate(self) -> gcp.certificatemanager.Certificate:
        """The wildcard certificate, issued once the authorisation CNAME this run writes into
        the apps zone resolves. The label is opaque because certificate logs are public."""
        wildcard = n.cell_wildcard(self.cfg.label)
        auth = gcp.certificatemanager.DnsAuthorization(
            "cert-dns-auth",
            project=self.pid,
            name="ssc-cell",
            domain=n.host_suffix(self.cfg.label),
            opts=self._o(),
        )
        record = auth.dns_resource_records.apply(lambda records: records[0])
        self._record(
            "dns-cert-auth",
            record.apply(lambda r: r.name or ""),
            record.apply(lambda r: r.type or ""),
            record.apply(lambda r: r.data or ""),
        )
        certificate = gcp.certificatemanager.Certificate(
            "cert",
            project=self.pid,
            name="ssc-cell-wildcard",
            managed=gcp.certificatemanager.CertificateManagedArgs(
                domains=[wildcard], dns_authorizations=[auth.id]
            ),
            opts=self._o(),
        )
        self.certificate_map = gcp.certificatemanager.CertificateMap(
            "cert-map", project=self.pid, name=ENTRY, opts=self._o()
        )
        gcp.certificatemanager.CertificateMapEntry(
            "cert-map-entry",
            project=self.pid,
            name="ssc-wildcard",
            map=self.certificate_map.name,
            hostname=wildcard,
            certificates=[certificate.id],
            opts=self._o(),
        )
        return certificate

    def _record(
        self,
        name: str,
        dns_name: pulumi.Input[str],
        kind: pulumi.Input[str],
        value: pulumi.Input[str],
    ) -> None:
        """A record in the platform's apps zone, written by this stack (no hand step)."""
        gcp.dns.RecordSet(
            name,
            project=n.BOOTSTRAP_PROJECT,
            managed_zone=n.APPS_ZONE,
            name=dns_name,
            type=kind,
            ttl=DNS_TTL,
            rrdatas=[value],
            opts=self._o(),
        )

    def cell_agent(self) -> None:
        """Runs ``python -m ssc_agent`` (decision 014) once ``agent_image`` names a build of
        ``packages/ssc_agent/Dockerfile`` in the cell's ``ssc-platform`` repository. Reached only
        through the cell's load balancer on its reserved host, and invoked only by the control
        plane's API and worker (SSC-064), with an ID token whose audience is that host's URL
        (SSC-095). With both build images set it runs builds in the cell's Cloud Build as
        ``ssc-build`` (SSC-015); with the ``database`` flag it makes app databases on the cell's
        instance (SSC-040). It reads app usage from the cell's Cloud Monitoring (SSC-028) and app
        logs through the two log views (SSC-024), and keeps under Cloud Logging's read quota per
        instance, so it runs one; its concurrency holds the 40 follows the agent allows at once
        beside every other call, and its timeout outlasts a 20 s follow and a 240 s traffic
        switch. It creates connection secrets with the connection tag, readable by ``ssc-data``
        alone (SSC-051). It writes each app environment's egress proxy credential into its
        ``HTTPS_PROXY`` secret, naming the proxy's reserved address, and tells the console the
        cell's fixed outbound address (SSC-053); both exist from onboarding. It deletes a gone
        environment's files from the cell bucket's ``files/`` (SSC-046)."""
        tag_key, tag_value = self.connection_tag
        env: dict[str, pulumi.Input[str]] = {
            "SSC_CELL_PROJECT": self.pid,
            "SSC_CELL_REGION": n.REGION,
            "SSC_CELL_NETWORK": self.vpc.id,
            "SSC_CELL_SUBNETWORK": self.apps_subnet.id,
            "SSC_IMAGE_REPOSITORY": self.app_images,
            "SSC_GATEWAY_SA": self.gateway_sa.email,
            LOG_VIEW_ENV: ",".join(self.log_view_names),
            USAGE_SOURCE_ENV: USAGE_SOURCE,
            DATA_SA_ENV: self.data_sa.email,
            CONNECTION_TAG_ENV: pulumi.Output.concat(tag_key, "=", tag_value),
            PROXY_ADDRESS_ENV: self.proxy_ip.address,
            OUTBOUND_IP_ENV: self.nat_ip.address,
            BUCKET_ENV: self.bucket_.name,
        }
        tools, frontend = self.cfg.build_tools_image, self.cfg.build_frontend_image
        if tools and frontend:
            env |= {
                "SSC_BUILD_SA": self.build_sa.email,
                "SSC_BUILD_TOOLS_IMAGE": tools,
                "SSC_BUILD_FRONTEND_IMAGE": frontend,
            }
        if self.cfg.database:
            env[n.SQL_INSTANCE_ENV] = self.sql.name
        self.agent_ = self._run(
            n.CELL_AGENT,
            self.agent_sa,
            ingress="INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
            vpc=None,
            instances=(0, AGENT_MAX),
            image=self.cfg.agent_image,
            env=env if self.cfg.agent_image else None,
            timeout=AGENT_TIMEOUT,
            concurrency=AGENT_CONCURRENCY,
            audiences=[n.agent_url(self.cfg.label)],
        )
        gcp.compute.SubnetworkIAMMember(
            "agent-apps-subnet",
            project=self.pid,
            region=n.REGION,
            subnetwork=self.apps_subnet.name,
            role="roles/compute.networkUser",
            member=self.agent_sa.member,
            opts=self._o(),
        )
        for name, account in (
            ("agent-invoker", self.control_sa),
            ("agent-invoker-worker", self.control_worker),
        ):
            gcp.cloudrunv2.ServiceIamMember(
                name,
                project=self.pid,
                location=n.REGION,
                name=self.agent_.name,
                role="roles/run.invoker",
                member=pulumi.Output.concat("serviceAccount:", account),
                opts=self._o(),
            )

    def secret_intake(self) -> None:
        """Runs ``python -m ssc_agent.intake`` from ``agent_image`` (SSC-026): the one door a
        secret value comes in by. Public on its reserved host through the cell's load balancer,
        where the CLI sends the value with the control plane's write grant, which the intake
        checks itself. No VPC egress, so it reaches Google's certificates and Secret Manager as
        the agent does, never through the apps' network."""
        cfg = self.cfg
        env: dict[str, pulumi.Input[str]] = {
            "SSC_CELL_PROJECT": self.pid,
            "SSC_INTAKE_ORIGIN": n.intake_url(cfg.label),
            "SSC_CONTROL_SA": self.control_sa,
        }
        self.intake_ = self._run(
            n.SECRET_INTAKE,
            self.intake_sa,
            ingress="INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
            vpc=None,
            instances=(0, INTAKE_MAX),
            image=cfg.agent_image,
            command=INTAKE_COMMAND if cfg.agent_image else None,
            env=env if cfg.agent_image else None,
        )
        self._public("intake", self.intake_)

    def proxy_check(self) -> None:
        """The proxy's health check, written at onboarding like its firewall rules: it costs
        nothing, and the cell deployer may then turn ``egress`` on with no load-balancing role."""
        self.proxy_health = gcp.compute.HealthCheck(
            "proxy-health",
            project=self.pid,
            name="ssc-proxy",
            check_interval_sec=PROXY_CHECK_SECONDS,
            timeout_sec=5,
            healthy_threshold=2,
            unhealthy_threshold=3,
            tcp_health_check=gcp.compute.HealthCheckTcpHealthCheckArgs(port=PROXY_PORT),
            log_config=gcp.compute.HealthCheckLogConfigArgs(enable=True),
            opts=self._o(),
        )

    def proxy(self) -> None:
        """The ``egress`` flag (SSC-053): one ``e2-micro`` at the reserved address, with no
        external address, in a group of one that recreates it when its TCP check on the proxy
        port fails. With ``proxy_image`` its unit runs the proxy, which reads the customer's
        snapshot as ``ssc-proxy`` and leaves through the cell's NAT. Its template takes a new
        name on every change, so a new image replaces the machine. ``proxy_ha`` runs two in two
        zones instead, behind an internal load balancer that holds the reserved address."""
        cfg = self.cfg
        health = self.proxy_health
        metadata = {
            "enable-oslogin": "TRUE",
            "block-project-ssh-keys": "true",
            "google-logging-enabled": "true",
            "cos-update-strategy": "update_disabled",
        }
        if cfg.proxy_image and cfg.org_id:
            metadata["user-data"] = proxy_cloud_config(
                cfg.proxy_image, cfg.org_id, n.cell_bucket(cfg.label)
            )
        template = gcp.compute.InstanceTemplate(
            "proxy-template",
            project=self.pid,
            name_prefix="ssc-proxy-",
            machine_type=PROXY_MACHINE,
            tags=[PROXY_TAG],
            disks=[
                gcp.compute.InstanceTemplateDiskArgs(
                    source_image=PROXY_BOOT_IMAGE, boot=True, auto_delete=True
                )
            ],
            network_interfaces=[
                gcp.compute.InstanceTemplateNetworkInterfaceArgs(
                    network=self.vpc.id,
                    subnetwork=self.edge_subnet.id,
                    network_ip=None if cfg.proxy_ha else self.proxy_ip.address,
                    stack_type="IPV4_ONLY",
                )
            ],
            service_account=gcp.compute.InstanceTemplateServiceAccountArgs(
                email=self.proxy_sa.email, scopes=["cloud-platform"]
            ),
            shielded_instance_config=gcp.compute.InstanceTemplateShieldedInstanceConfigArgs(
                enable_secure_boot=True, enable_vtpm=True, enable_integrity_monitoring=True
            ),
            metadata=metadata,
            labels={n.CELL_LABEL_KEY: cfg.label},
            opts=self._o(),
        )
        if cfg.proxy_ha:
            self._proxy_ha(template, health)
            return
        gcp.compute.InstanceGroupManager(
            "proxy",
            project=self.pid,
            name="ssc-proxy",
            zone=PROXY_ZONE,
            base_instance_name="ssc-proxy",
            target_size=1,
            versions=[
                gcp.compute.InstanceGroupManagerVersionArgs(instance_template=template.self_link)
            ],
            auto_healing_policies=gcp.compute.InstanceGroupManagerAutoHealingPoliciesArgs(
                health_check=health.id, initial_delay_sec=PROXY_HEAL_DELAY
            ),
            update_policy=gcp.compute.InstanceGroupManagerUpdatePolicyArgs(
                type="PROACTIVE",
                minimal_action="REPLACE",
                replacement_method="RECREATE",
                max_surge_fixed=0,
                max_unavailable_fixed=1,
            ),
            wait_for_instances=False,
            opts=self._o(),
        )

    def _proxy_ha(
        self, template: gcp.compute.InstanceTemplate, health: gcp.compute.HealthCheck
    ) -> None:
        """Two machines in two zones, each replaced only once its new one exists, behind an
        internal passthrough load balancer on the proxy port at the reserved address."""
        group = gcp.compute.RegionInstanceGroupManager(
            "proxy-ha",
            project=self.pid,
            name="ssc-proxy",
            region=n.REGION,
            base_instance_name="ssc-proxy",
            target_size=PROXY_HA_SIZE,
            distribution_policy_zones=[f"{n.REGION}-a", f"{n.REGION}-b"],
            versions=[
                gcp.compute.RegionInstanceGroupManagerVersionArgs(
                    instance_template=template.self_link
                )
            ],
            auto_healing_policies=gcp.compute.RegionInstanceGroupManagerAutoHealingPoliciesArgs(
                health_check=health.id, initial_delay_sec=PROXY_HEAL_DELAY
            ),
            update_policy=gcp.compute.RegionInstanceGroupManagerUpdatePolicyArgs(
                type="PROACTIVE",
                minimal_action="REPLACE",
                instance_redistribution_type="PROACTIVE",
                replacement_method="SUBSTITUTE",
                max_surge_fixed=PROXY_HA_SIZE,
                max_unavailable_fixed=0,
            ),
            wait_for_instances=False,
            opts=self._o(),
        )
        backend = gcp.compute.RegionBackendService(
            "proxy-ha",
            project=self.pid,
            name="ssc-proxy",
            region=n.REGION,
            protocol="TCP",
            load_balancing_scheme="INTERNAL",
            health_checks=health.id,
            backends=[
                gcp.compute.RegionBackendServiceBackendArgs(
                    group=group.instance_group, balancing_mode="CONNECTION"
                )
            ],
            opts=self._o(),
        )
        gcp.compute.ForwardingRule(
            "proxy-ha",
            project=self.pid,
            name="ssc-proxy",
            region=n.REGION,
            load_balancing_scheme="INTERNAL",
            ip_protocol="TCP",
            ports=[str(PROXY_PORT)],
            ip_address=self.proxy_ip.address,
            network=self.vpc.id,
            subnetwork=self.edge_subnet.id,
            backend_service=backend.id,
            opts=self._o(),
        )

    def data_gateway(self) -> None:
        """The ``connections`` flag: the data gateway and file broker, leaving through the NAT.
        The first deploy whose manifest asks for ``[files]`` sets it, as the first that asks for
        a database sets ``database`` (SSC-046, SSC-087); every grant the broker needs was made
        at onboarding, so no person acts.

        Ingress is internal only, so only the cell's VPC reaches it. Cloud Run's invoker check is
        off: the gateway checks each caller's Google ID token itself and admits only the cell's
        app accounts (SSC-050, ``ssc_datagw.workload``), so no app needs ``run.invoker`` and no
        ``allUsers`` grant or public-invoker tag is involved. With ``datagw_image`` it runs a
        build of ``packages/ssc_datagw/Dockerfile``, which reads its snapshot from the cell
        bucket's ``snapshots/`` (``bucket-data``). Each connection in ``datagw_connections``
        becomes ``SSC_CONNECTION_CON_<20>``, the secret ``ssc-conn-<20>`` at its pinned version
        (SSC-051). The file broker signs its links as ``ssc-data`` through ``signBlob`` on
        itself (``data-signs-as-itself``), with no key file, and reads and writes the bucket's
        ``files/`` alone (``bucket-data-files``)."""
        env = self._datagw_env()
        secret_env = {
            connection_env(c): (connection_secret_id(c), version)
            for c, version in connection_versions(self.cfg.datagw_connections).items()
        }
        self.data_gateway_ = self._run(
            n.DATA_GATEWAY,
            self.data_sa,
            ingress="INGRESS_TRAFFIC_INTERNAL_ONLY",
            vpc=self._edge_vpc(DATA_TAG),
            instances=(0, DATAGW_MAX),
            image=self.cfg.datagw_image,
            env=env,
            secret_env=secret_env if env else None,
            invoker_iam=False,
        )

    def _datagw_env(self) -> dict[str, pulumi.Input[str]] | None:
        """What ``ssc_datagw.settings.settings_from_env`` reads, once ``datagw_image`` is set.
        ``SSC_DATAGW_AUDIENCE`` is the service's own ``run.app`` URL, the audience apps mint
        their workload token for."""
        cfg = self.cfg
        if not (cfg.datagw_image and cfg.org_id and cfg.gateway_jwks):
            return None
        return {
            "SSC_ORG_ID": cfg.org_id,
            "SSC_CELL_LABEL": cfg.label,
            "SSC_PROJECT_ID": cfg.project_id,
            "SSC_CELL_BUCKET": self.bucket_.name,
            "SSC_DATAGW_AUDIENCE": self.project_.number.apply(
                lambda p: n.run_url(n.DATA_GATEWAY, p)
            ),
            "SSC_IDENTITY_JWKS": cfg.gateway_jwks,
            "SSC_APPS_DOMAIN": n.APPS_DOMAIN,
            "SSC_IDENTITY_ISSUER": n.identity_issuer(cfg.label),
        }

    def deny(self) -> None:
        """The folder rule names the control plane; this one names the cell's own identities.

        The first rule refuses every secret value to all of them but ``ssc-data``, whatever is
        granted. The second refuses ``ssc-data`` every secret without the cell's connection tag
        (SSC-051), so no grant can give it an app secret; it reads a connection secret only with
        the ``secretAccessor`` the agent sets on that secret."""
        ours = (self.gateway_sa, self.agent_sa, self.build_sa, self.intake_sa, self.proxy_sa)
        denied = [sa_principal(sa.email) for sa in ours]
        if self.cfg.probe:
            self.denied_probe = self._sa(n.PROBE_DENIED_SA, "SSC deny probe (always refused)")
            denied.append(sa_principal(self.denied_probe.email))
        tag_key, tag_value = self.connection_tag
        gcp.iam.DenyPolicy(
            "cell-secret-read",
            parent=pulumi.Output.concat(
                "cloudresourcemanager.googleapis.com%2Fprojects%2F", self.pid
            ),
            name="ssc-deny-secret-read",
            display_name="SSC identities never read secret values",
            rules=[
                gcp.iam.DenyPolicyRuleArgs(
                    description="Only an app's own identity reads its secrets.",
                    deny_rule=gcp.iam.DenyPolicyRuleDenyRuleArgs(
                        denied_principals=denied, denied_permissions=[n.SECRET_READ]
                    ),
                ),
                gcp.iam.DenyPolicyRuleArgs(
                    description="The data gateway reads connection secrets and no other.",
                    deny_rule=gcp.iam.DenyPolicyRuleDenyRuleArgs(
                        denied_principals=[sa_principal(self.data_sa.email)],
                        denied_permissions=[n.SECRET_READ],
                        denial_condition=gcp.iam.DenyPolicyRuleDenyRuleDenialConditionArgs(
                            title=f"not {SECRET_KIND_KEY}={CONNECTION_KIND}",
                            expression=pulumi.Output.format(
                                "!resource.matchTagId('{0}', '{1}')", tag_key, tag_value
                            ),
                        ),
                    ),
                ),
            ],
            opts=self._o(),
        )

    def probe(self) -> None:
        """A secret both probe identities hold ``secretAccessor`` on: one must read it, the other
        must be refused by the deny rule alone. The value is random and never stored."""
        secret = gcp.secretmanager.Secret(
            "probe-secret",
            project=self.pid,
            secret_id=n.PROBE_SECRET,
            replication=gcp.secretmanager.SecretReplicationArgs(
                user_managed=gcp.secretmanager.SecretReplicationUserManagedArgs(
                    replicas=[
                        gcp.secretmanager.SecretReplicationUserManagedReplicaArgs(location=n.REGION)
                    ]
                )
            ),
            deletion_protection=False,
            opts=self._o(),
        )
        gcp.secretmanager.SecretVersion(
            "probe-secret-v1",
            secret=secret.id,
            secret_data_wo=secrets.token_urlsafe(32),
            secret_data_wo_version=1,
            opts=self._o(),
        )
        allowed = self._sa(n.PROBE_ALLOWED_SA, "SSC deny probe (control, may read)")
        for name, sa in ((n.PROBE_ALLOWED_SA, allowed), (n.PROBE_DENIED_SA, self.denied_probe)):
            gcp.secretmanager.SecretIamMember(
                f"probe-read-{name}",
                project=self.pid,
                secret_id=secret.secret_id,
                role="roles/secretmanager.secretAccessor",
                member=sa.member,
                opts=self._o(),
            )
            gcp.serviceaccount.IAMMember(
                f"probe-operator-{name}",
                service_account_id=sa.name,
                role="roles/iam.serviceAccountTokenCreator",
                member=n.OPERATOR,
                opts=self._o(),
            )

    def probe_runner(self) -> None:
        """The in-cell probe run (SSC-017): a job that stands where the gateway stands (its
        identity, subnet and tag) and calls probe app ``a``. The nightly run starts it. The app
        also dials the platform hosts that resolve in the cell, which must stay unreachable. The
        nightly account also reads the cell's snapshots, which the kill drill times (SSC-054)."""
        digest = self.cfg.probe_digest
        if digest is None:
            return
        a, b = (service_name(env) for env in n.PROBE_ENVS)
        number = self.project_.number
        job = gcp.cloudrunv2.Job(
            "probe-runner",
            project=self.pid,
            name=n.PROBE_RUNNER,
            location=n.REGION,
            deletion_protection=False,
            template=gcp.cloudrunv2.JobTemplateArgs(
                task_count=1,
                template=gcp.cloudrunv2.JobTemplateTemplateArgs(
                    service_account=self.gateway_sa.email,
                    max_retries=0,
                    timeout="600s",
                    vpc_access=gcp.cloudrunv2.JobTemplateTemplateVpcAccessArgs(
                        egress="ALL_TRAFFIC",
                        network_interfaces=[
                            gcp.cloudrunv2.JobTemplateTemplateVpcAccessNetworkInterfaceArgs(
                                network=self.vpc.id,
                                subnetwork=self.edge_subnet.id,
                                tags=[GATEWAY_TAG],
                            )
                        ],
                    ),
                    containers=[
                        gcp.cloudrunv2.JobTemplateTemplateContainerArgs(
                            image=pulumi.Output.concat(self.app_images, "@", digest),
                            commands=["python", "/app/runner.py"],
                            envs=[
                                gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs(
                                    name="PROBE_URL", value=number.apply(lambda p: n.run_url(a, p))
                                ),
                                gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs(
                                    name="PROBE_PEER_URL",
                                    value=number.apply(lambda p: n.run_url(b, p)),
                                ),
                                gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs(
                                    name="PROBE_EGRESS_HOSTS",
                                    value=",".join(n.GATEWAY_PLATFORM_HOSTS),
                                ),
                            ],
                        )
                    ],
                ),
            ),
            opts=self._o(),
        )
        nightly = self.platform.require_output("nightly_service_account")
        member = pulumi.Output.concat("serviceAccount:", nightly)
        gcp.cloudrunv2.JobIamMember(
            "probe-runner-nightly",
            project=self.pid,
            location=n.REGION,
            name=job.name,
            role="roles/run.jobsExecutor",
            member=member,
            opts=self._o(),
        )
        # Reads the run's executions and the probe results it logs; staging probe cells only.
        self._project_role("nightly-run-viewer", member, "roles/run.viewer")
        self._project_role("nightly-logs", member, "roles/logging.viewer")
        snapshots = f"projects/_/buckets/{n.cell_bucket(self.cfg.label)}/objects/{SNAPSHOTS_PREFIX}"
        gcp.storage.BucketIAMMember(
            "bucket-nightly-snapshots",
            bucket=self.bucket_.name,
            role="roles/storage.objectViewer",
            member=member,
            condition=gcp.storage.BucketIAMMemberConditionArgs(
                title="only snapshots",
                expression=f'resource.name.startsWith("{snapshots}")',
            ),
            opts=self._o(),
        )

    def exports(self) -> None:
        pulumi.export("project_id", self.pid)
        pulumi.export("project_number", self.project_.number)
        pulumi.export("bucket", self.bucket_.name)
        if self.cfg.database:
            pulumi.export("sql_instance", self.sql.connection_name)
        pulumi.export("registry", self.repo.name)
        pulumi.export("app_images", self.app_images)
        pulumi.export("agent_url", n.agent_url(self.cfg.label))
        pulumi.export("entry_address", self.entry_ip.address)
        pulumi.export("public_host_suffix", n.host_suffix(self.cfg.label))
        pulumi.export("certificate_id", self.certificate.id)
        pulumi.export("agent_host", n.agent_host(self.cfg.label))
        pulumi.export("intake_url", n.intake_url(self.cfg.label))
        pulumi.export("intake_host", n.intake_host(self.cfg.label))
        pulumi.export("nat_ip", self.nat_ip.address)
        pulumi.export("proxy_ip", self.proxy_ip.address)
        pulumi.export("datagw_ip", self.datagw_ip.address)
        pulumi.export("database_range", f"{PSA_ADDRESS}/{PSA_PREFIX}")
        pulumi.export("gateway_kms_key", self.gateway_key.id)
        if self.cfg.gateway_jwks:
            pulumi.export("identity_jwks", self.cfg.gateway_jwks)
        pulumi.export("flags", self.cfg.flags)
        pulumi.export("config", self.cfg.settings)
        pulumi.export(
            "service_accounts",
            {
                "gateway": self.gateway_sa.email,
                "agent": self.agent_sa.email,
                "build": self.build_sa.email,
                "data": self.data_sa.email,
                "intake": self.intake_sa.email,
                "proxy": self.proxy_sa.email,
            },
        )


def build(stack: str) -> None:
    Cell(read_config(stack), pulumi.StackReference(n.platform_stack_ref())).build()


def sql_dns_name(names: object, legacy: object) -> str:
    """The instance's private services access name, fully qualified: Cloud SQL lists it in
    ``dnsNames`` and leaves ``dnsName`` empty for such an instance."""
    entries = cast("list[object]", names) if isinstance(names, list) else []
    for entry in entries:
        fields = cast("dict[str, object]", entry) if isinstance(entry, dict) else {}
        kind = fields.get("connection_type", fields.get("connectionType"))
        name = str(fields.get("name") or "")
        if kind == "PRIVATE_SERVICES_ACCESS" and name:
            return name if name.endswith(".") else f"{name}."
    name = str(legacy or "")
    if not name:
        raise ValueError("the database instance has no DNS name")
    return name if name.endswith(".") else f"{name}."


def tlds() -> list[str]:
    lines = TLDS_FILE.read_text(encoding="ascii").splitlines()
    return [line.lower() for line in lines if line and not line.startswith("#")]


def log_bucket(project_id: str) -> str:
    """The project's ``_Default`` log bucket, in the region its folder's log setting names."""
    return f"projects/{project_id}/locations/{n.REGION}/buckets/{LOG_BUCKET}"


def edge_address(host: int) -> str:
    """Address ``host`` of the edge subnet, reserved at cell creation."""
    return str(ip_network(SUBNETS[EDGE_SUBNET])[host])


def internal_ranges() -> list[str]:
    """What an untagged app may reach inside the cell (SSC-027): the proxy and data gateway
    addresses and the database's private range, written once whether or not they exist yet."""
    hosts = [f"{edge_address(host)}/32" for host in (PROXY_HOST, DATAGW_HOST)]
    return [*hosts, f"{PSA_ADDRESS}/{PSA_PREFIX}"]
