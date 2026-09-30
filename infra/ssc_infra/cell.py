"""A ``c-<cell label>`` stack: one customer cell, one project (SSC-013, decisions 021 and 022).

Everything a cell holds is named from the label alone, so two cells differ only in their label,
project number and addresses; ``python -m ssc_infra.cell_diff`` checks exactly that.
"""

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import pulumi
import pulumi_gcp as gcp

from ssc_infra import naming as n
from ssc_infra.platform import provider, sa_principal

APIS: Final = (
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudkms.googleapis.com",
    "compute.googleapis.com",
    "dns.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
)
APPS_RANGE: Final = "10.20.0.0/22"
GATEWAY_RANGE: Final = "10.20.4.0/24"
PROXY_RANGE: Final = "10.20.6.0/23"
CELL_RANGE: Final = "10.20.0.0/16"
PSA_ADDRESS: Final = "10.21.0.0"
PSA_PREFIX: Final = 20
GOOGLE_PRIVATE: Final = ("199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11")
GOOGLE_PRIVATE_RANGE: Final = "199.36.153.8/30"
GATEWAY_TAG: Final = "ssc-gateway"
KEY_ROTATION: Final = "7776000s"
SQL_TIER: Final = "db-custom-1-3840"
PLACEHOLDER_IMAGE: Final = "us-docker.pkg.dev/cloudrun/container/hello"
APP_CONDITION: Final = 'resource.name.extract("/{kind}/{{name}}").startsWith("{prefix}")'
CREATE_PERMISSIONS: Final = (
    "iam.serviceAccounts.create",
    "run.services.create",
    "secretmanager.secrets.create",
    "cloudsql.databases.create",
)


@dataclass(frozen=True, slots=True)
class CellConfig:
    label: str
    stage: n.Stage
    probe: bool
    gateway_min: int

    @property
    def project_id(self) -> str:
        return n.cell_project(self.label)

    @property
    def disposable(self) -> bool:
        return self.stage == "staging"


def read_config(stack: str) -> CellConfig:
    config = pulumi.Config()
    stage = config.get("stage") or "staging"
    if stage != "prod" and stage != "staging":  # noqa: PLR1714  (narrows to Stage)
        raise ValueError(f"stage must be one of {n.STAGES}, not {stage!r}")
    return CellConfig(
        label=n.label_of_stack(stack),
        stage=stage,
        probe=config.get_bool("probe") or False,
        gateway_min=config.get_int("gateway_min") or 2,
    )


def app_condition(kind: str) -> gcp.projects.IAMMemberConditionArgs:
    return gcp.projects.IAMMemberConditionArgs(
        title=f"only {n.APP_PREFIX}* {kind}",
        expression=APP_CONDITION.format(kind=kind, prefix=n.APP_PREFIX),
    )


def control_for(accounts: dict[str, str], stage: n.Stage) -> str:
    if stage not in accounts:
        raise ValueError(f"the platform stack has no {stage} control plane yet")
    return accounts[stage]


class Cell:
    """Builds the resources in dependency order; each step keeps what later steps need."""

    def __init__(self, cfg: CellConfig, platform: pulumi.StackReference) -> None:
        self.cfg = cfg
        self.platform = platform
        self.opts = pulumi.ResourceOptions(provider=provider())

    def _o(self, *depends_on: pulumi.Resource) -> pulumi.ResourceOptions:
        return pulumi.ResourceOptions.merge(
            self.opts, pulumi.ResourceOptions(depends_on=list(depends_on))
        )

    def build(self) -> None:
        self.project()
        self.identities()
        self.keys()
        self.network()
        self.database()
        self.registry()
        self.bucket()
        self.gateway()
        self.cell_agent()
        self.load_balancer()
        self.deny()
        if self.cfg.probe:
            self.probe()
        self.exports()

    def project(self) -> None:
        cfg = self.cfg
        folder = self.platform.require_output("stage_folder_ids").apply(lambda f: f[cfg.stage])
        self.project_ = gcp.organizations.Project(
            "project",
            project_id=cfg.project_id,
            name=cfg.project_id,
            folder_id=folder,
            billing_account=n.BILLING_ACCOUNT,
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
        self.control_sa = self.platform.require_output("control_service_accounts").apply(
            lambda m: control_for(m, cfg.stage)
        )

    def _sa(self, account: str, display: str) -> gcp.serviceaccount.Account:
        return gcp.serviceaccount.Account(
            account,
            project=self.pid,
            account_id=account,
            display_name=display,
            opts=self._o(*self.apis),
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
            opts=self._o(*self.apis),
        )

    def identities(self) -> None:
        self.gateway_sa = self._sa("ssc-gateway", "SSC cell gateway")
        self.agent_sa = self._sa("ssc-cell-agent", "SSC cell agent")
        self.build_sa = self._sa("ssc-build", "SSC builds")
        agent = self.agent_sa.member
        create_role = gcp.projects.IAMCustomRole(
            "agent-create",
            project=self.pid,
            role_id="sscCellAgentCreate",
            title="SSC cell agent: create app resources",
            description="Create calls cannot be limited by name in IAM; the agent's code does it.",
            permissions=list(CREATE_PERMISSIONS),
            opts=self._o(*self.apis),
        )
        gcp.projects.IAMMember(
            "agent-create",
            project=self.pid,
            member=agent,
            role=create_role.name,
            opts=self._o(*self.apis),
        )
        for name, role, kind in (
            ("agent-secrets", "roles/secretmanager.admin", "secrets"),
            ("agent-run", "roles/run.admin", "services"),
            ("agent-sas", "roles/iam.serviceAccountAdmin", "serviceAccounts"),
            ("agent-actas", "roles/iam.serviceAccountUser", "serviceAccounts"),
        ):
            self._project_role(name, agent, role, app_condition(kind))
        self._project_role("agent-sql", agent, "roles/cloudsql.admin")
        self._project_role("agent-sql-client", agent, "roles/cloudsql.client")
        self._project_role("agent-sql-login", agent, "roles/cloudsql.instanceUser")
        self._project_role(
            "control-secret-versions",
            pulumi.Output.concat("serviceAccount:", self.control_sa),
            "roles/secretmanager.secretVersionAdder",
            app_condition("secrets"),
        )
        self._project_role("build-logs", self.build_sa.member, "roles/logging.logWriter")
        self._project_role("gateway-logs", self.gateway_sa.member, "roles/logging.logWriter")
        self._project_role("agent-logs", agent, "roles/logging.logWriter")

    def keys(self) -> None:
        ring = gcp.kms.KeyRing(
            "keyring",
            project=self.pid,
            name="ssc-cell",
            location=n.REGION,
            opts=self._o(*self.apis),
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
        self.key_grants: list[pulumi.Resource] = []
        for name, service, key in (
            ("sql", "sqladmin.googleapis.com", self.sql_key),
            ("registry", "artifactregistry.googleapis.com", self.registry_key),
        ):
            agent = gcp.projects.ServiceIdentity(
                f"{name}-agent", project=self.pid, service=service, opts=self._o(*self.apis)
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
            opts=self._o(*self.apis),
        )
        self.apps_subnet = self._subnet("apps", APPS_RANGE)
        self.gateway_subnet = self._subnet("gateway", GATEWAY_RANGE)
        self.proxy_subnet = gcp.compute.Subnetwork(
            "subnet-proxy",
            project=self.pid,
            name="proxy-only",
            region=n.REGION,
            network=self.vpc.id,
            ip_cidr_range=PROXY_RANGE,
            purpose="REGIONAL_MANAGED_PROXY",
            role="ACTIVE",
            opts=self._o(),
        )
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
            opts=self._o(),
        )
        psa_cidr = f"{PSA_ADDRESS}/{PSA_PREFIX}"
        self._egress("egress-deny-all", 65534, deny=True, ranges=["0.0.0.0/0"])
        self._egress("egress-internal", 1000, ranges=[CELL_RANGE, psa_cidr])
        self._egress("egress-google-private", 1000, ranges=[GOOGLE_PRIVATE_RANGE])
        self._egress("egress-gateway", 1000, ranges=["0.0.0.0/0"], tags=[GATEWAY_TAG])
        self._private_google_dns()
        self.nat_ips: dict[str, gcp.compute.Address] = {}
        router = gcp.compute.Router(
            "router",
            project=self.pid,
            name="ssc-cell",
            region=n.REGION,
            network=self.vpc.id,
            opts=self._o(),
        )
        for name, subnet in (("apps", self.apps_subnet), ("gateway", self.gateway_subnet)):
            ip = gcp.compute.Address(
                f"nat-ip-{name}",
                project=self.pid,
                name=f"nat-{name}",
                region=n.REGION,
                address_type="EXTERNAL",
                network_tier="PREMIUM",
                opts=self._o(),
            )
            gcp.compute.RouterNat(
                f"nat-{name}",
                project=self.pid,
                name=f"nat-{name}",
                region=n.REGION,
                router=router.name,
                nat_ip_allocate_option="MANUAL_ONLY",
                nat_ips=[ip.self_link],
                source_subnetwork_ip_ranges_to_nat="LIST_OF_SUBNETWORKS",
                subnetworks=[
                    gcp.compute.RouterNatSubnetworkArgs(
                        name=subnet.id, source_ip_ranges_to_nats=["ALL_IP_RANGES"]
                    )
                ],
                opts=self._o(),
            )
            self.nat_ips[name] = ip

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
            opts=self._o(),
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

    def database(self) -> None:
        cfg = self.cfg
        self.sql = gcp.sql.DatabaseInstance(
            "sql",
            project=self.pid,
            name="ssc-cell",
            region=n.REGION,
            database_version="POSTGRES_18",
            encryption_key_name=self.sql_key.id,
            deletion_protection=not cfg.disposable,
            settings=gcp.sql.DatabaseInstanceSettingsArgs(
                tier=SQL_TIER,
                edition="ENTERPRISE",
                availability_type="REGIONAL",
                deletion_protection_enabled=not cfg.disposable,
                data_api_access="ALLOW_DATA_API",
                ip_configuration=gcp.sql.DatabaseInstanceSettingsIpConfigurationArgs(
                    ipv4_enabled=False, private_network=self.vpc.id, ssl_mode="ENCRYPTED_ONLY"
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
                    )
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
        gcp.artifactregistry.RepositoryIamMember(
            "registry-build",
            project=self.pid,
            location=n.REGION,
            repository=self.repo.name,
            role="roles/artifactregistry.writer",
            member=self.build_sa.member,
            opts=self._o(),
        )

    def bucket(self) -> None:
        cfg = self.cfg
        self.bucket_ = gcp.storage.Bucket(
            "bucket",
            project=self.pid,
            name=n.cell_bucket(cfg.label),
            location=n.REGION.upper(),
            uniform_bucket_level_access=True,
            public_access_prevention="enforced",
            versioning=gcp.storage.BucketVersioningArgs(enabled=True),
            force_destroy=cfg.disposable,
            opts=self._o(*self.apis),
        )
        for name, member, role in (
            (
                "bucket-control",
                pulumi.Output.concat("serviceAccount:", self.control_sa),
                "roles/storage.objectUser",
            ),
            ("bucket-gateway", self.gateway_sa.member, "roles/storage.objectViewer"),
            ("bucket-agent", self.agent_sa.member, "roles/storage.objectViewer"),
        ):
            gcp.storage.BucketIAMMember(
                name, bucket=self.bucket_.name, role=role, member=member, opts=self._o()
            )

    def _run(
        self,
        name: str,
        sa: gcp.serviceaccount.Account,
        ingress: str,
        vpc: gcp.cloudrunv2.ServiceTemplateVpcAccessArgs | None,
        min_instances: int,
    ) -> gcp.cloudrunv2.Service:
        return gcp.cloudrunv2.Service(
            name,
            project=self.pid,
            name=name,
            location=n.REGION,
            ingress=ingress,
            deletion_protection=not self.cfg.disposable,
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=sa.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=min_instances),
                vpc_access=vpc,
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=PLACEHOLDER_IMAGE,
                        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                            cpu_idle=min_instances == 0,
                            limits={"cpu": "1", "memory": "512Mi"},
                        ),
                    )
                ],
            ),
            opts=self._o(*self.apis),
        )

    def gateway(self) -> None:
        self.gateway_ = self._run(
            "ssc-gateway",
            self.gateway_sa,
            "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
            gcp.cloudrunv2.ServiceTemplateVpcAccessArgs(
                egress="ALL_TRAFFIC",
                network_interfaces=[
                    gcp.cloudrunv2.ServiceTemplateVpcAccessNetworkInterfaceArgs(
                        network=self.vpc.id, subnetwork=self.gateway_subnet.id, tags=[GATEWAY_TAG]
                    )
                ],
            ),
            self.cfg.gateway_min,
        )

    def cell_agent(self) -> None:
        agent = self._run("ssc-cell-agent", self.agent_sa, "INGRESS_TRAFFIC_ALL", None, 0)
        gcp.cloudrunv2.ServiceIamMember(
            "agent-invoker",
            project=self.pid,
            location=n.REGION,
            name=agent.name,
            role="roles/run.invoker",
            member=pulumi.Output.concat("serviceAccount:", self.control_sa),
            opts=self._o(),
        )

    def load_balancer(self) -> None:
        neg = gcp.compute.RegionNetworkEndpointGroup(
            "gateway-neg",
            project=self.pid,
            name="ssc-gateway",
            region=n.REGION,
            network_endpoint_type="SERVERLESS",
            cloud_run=gcp.compute.RegionNetworkEndpointGroupCloudRunArgs(
                service=self.gateway_.name
            ),
            opts=self._o(),
        )
        backend = gcp.compute.RegionBackendService(
            "gateway-backend",
            project=self.pid,
            name="ssc-gateway",
            region=n.REGION,
            load_balancing_scheme="INTERNAL_MANAGED",
            protocol="HTTP",
            backends=[
                gcp.compute.RegionBackendServiceBackendArgs(
                    group=neg.id, balancing_mode="UTILIZATION", capacity_scaler=1.0
                )
            ],
            opts=self._o(),
        )
        url_map = gcp.compute.RegionUrlMap(
            "gateway-urlmap",
            project=self.pid,
            name="ssc-gateway",
            region=n.REGION,
            default_service=backend.id,
            opts=self._o(),
        )
        proxy = gcp.compute.RegionTargetHttpProxy(
            "gateway-proxy",
            project=self.pid,
            name="ssc-gateway",
            region=n.REGION,
            url_map=url_map.id,
            opts=self._o(),
        )
        self.lb = gcp.compute.ForwardingRule(
            "gateway-ilb",
            project=self.pid,
            name="ssc-gateway",
            region=n.REGION,
            load_balancing_scheme="INTERNAL_MANAGED",
            ip_protocol="TCP",
            port_range="80",
            target=proxy.id,
            network=self.vpc.id,
            subnetwork=self.gateway_subnet.id,
            network_tier="PREMIUM",
            opts=self._o(self.proxy_subnet),
        )

    def deny(self) -> None:
        """The folder rule names the control plane; this one names the cell's own identities."""
        denied = [sa_principal(sa.email) for sa in (self.gateway_sa, self.agent_sa, self.build_sa)]
        if self.cfg.probe:
            self.denied_probe = self._sa(n.PROBE_DENIED_SA, "SSC deny probe (always refused)")
            denied.append(sa_principal(self.denied_probe.email))
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
                )
            ],
            opts=self._o(*self.apis),
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
            opts=self._o(*self.apis),
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

    def exports(self) -> None:
        pulumi.export("project_id", self.pid)
        pulumi.export("project_number", self.project_.number)
        pulumi.export("bucket", self.bucket_.name)
        pulumi.export("sql_instance", self.sql.connection_name)
        pulumi.export("registry", self.repo.name)
        pulumi.export("gateway_ilb_ip", self.lb.ip_address)
        pulumi.export("nat_ips", {k: ip.address for k, ip in self.nat_ips.items()})
        pulumi.export(
            "service_accounts",
            {
                "gateway": self.gateway_sa.email,
                "agent": self.agent_sa.email,
                "build": self.build_sa.email,
            },
        )


def build(stack: str) -> None:
    Cell(read_config(stack), pulumi.StackReference(n.platform_stack_ref())).build()
