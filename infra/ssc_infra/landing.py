"""delimitus.com in the ``platform`` stack (SSC-065, decision 025): the landing page and the pilot
request form, ``python -m ssc_landing``, in a project of its own under the platform folder.

Two steps, both off until config turns them on, so an apply from ``main`` adds nothing before
the founder asks for it:

- ``landing_enabled: true``: the project, its registry (push the image there), the request
  bucket and the service's account, and the alert that tells the founder a request came in.
- ``landing_image: <registry>/ssc-landing@sha256:...``: the Cloud Run service, the external
  HTTPS load balancer with a Google-managed certificate for the apex and ``www``, and the A
  records in the ``delimitus-com`` zone, which lives in the Delimitus project
  (``landing_dns_project``) outside this program.

The service's account holds one role, on one bucket: create objects. It cannot read, list or
replace a stored request, and it has nothing in the control plane or any cell.
"""

from dataclasses import dataclass
from typing import Final

import pulumi
import pulumi_gcp as gcp

from ssc_infra import naming as n

PROJECT: Final = "ssc-site-0"
SERVICE: Final = "ssc-landing"
SA: Final = "ssc-landing"
REPOSITORY: Final = "site"
BUCKET: Final = f"{PROJECT}-pilot-requests"
APEX: Final = "delimitus.com"
WWW: Final = f"www.{APEX}"
ORIGIN: Final = f"https://{APEX}"
DNS_ZONE: Final = "delimitus-com"
RETENTION_DAYS: Final = 365
"""The page says 12 months; a request is deleted on the next lifecycle pass after a year."""
MAX_INSTANCES: Final = 3
"""The form allows 5 requests a minute per address in each instance."""
STORED_EVENT: Final = "pilot_request_stored"
FAILED_EVENT: Final = "pilot_request_not_stored"
APIS: Final = (
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
)


@dataclass(frozen=True, slots=True)
class LandingConfig:
    enabled: bool
    image: str | None
    dns_project: str | None
    dns_zone: str
    notify_email: str


class LandingConfigError(ValueError):
    pass


def landing_config(config: pulumi.Config) -> LandingConfig:
    image = config.get("landing_image") or None
    enabled = bool(config.get_bool("landing_enabled")) or image is not None
    dns_project = config.get("landing_dns_project") or None
    if image is not None:
        if "@sha256:" not in image:
            raise LandingConfigError("landing_image must name a digest: <image>@sha256:<hex>")
        if dns_project is None:
            raise LandingConfigError("landing_image needs landing_dns_project (the zone's project)")
    return LandingConfig(
        enabled=enabled,
        image=image,
        dns_project=dns_project,
        dns_zone=config.get("landing_dns_zone") or DNS_ZONE,
        notify_email=config.get("landing_notify_email") or n.OPERATOR.removeprefix("user:"),
    )


def log_filter(event: str) -> str:
    return (
        'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{SERVICE}" '
        f'AND jsonPayload.event="{event}"'
    )


class Landing:
    def __init__(
        self, cfg: LandingConfig, folder_id: pulumi.Input[str], opts: pulumi.ResourceOptions
    ) -> None:
        self.cfg = cfg
        self.opts = opts
        self.project = gcp.organizations.Project(
            "site",
            project_id=PROJECT,
            name=PROJECT,
            folder_id=folder_id,
            billing_account=n.BILLING_ACCOUNT,
            auto_create_network=False,
            deletion_policy="PREVENT",
            opts=opts,
        )
        self.pid = self.project.project_id
        self.apis = [
            gcp.projects.Service(
                f"site-{api.split('.')[0]}",
                project=self.pid,
                service=api,
                disable_on_destroy=False,
                opts=opts,
            )
            for api in APIS
        ]

    def _o(self) -> pulumi.ResourceOptions:
        return pulumi.ResourceOptions.merge(self.opts, pulumi.ResourceOptions(depends_on=self.apis))

    def base(self) -> None:
        self.repo = gcp.artifactregistry.Repository(
            "site-registry",
            project=self.pid,
            location=n.REGION,
            repository_id=REPOSITORY,
            format="DOCKER",
            description="The delimitus.com image (packages/ssc_landing/Dockerfile).",
            opts=self._o(),
        )
        self.sa = gcp.serviceaccount.Account(
            "site-sa",
            project=self.pid,
            account_id=SA,
            display_name="delimitus.com: creates pilot requests, reads nothing",
            opts=self._o(),
        )
        self.bucket = gcp.storage.Bucket(
            "site-requests",
            project=self.pid,
            name=BUCKET,
            location=n.REGION.upper(),
            uniform_bucket_level_access=True,
            public_access_prevention="enforced",
            # Deleted means gone: no versions and no soft-delete copies outlive the year.
            versioning=gcp.storage.BucketVersioningArgs(enabled=False),
            soft_delete_policy=gcp.storage.BucketSoftDeletePolicyArgs(retention_duration_seconds=0),
            lifecycle_rules=[
                gcp.storage.BucketLifecycleRuleArgs(
                    action=gcp.storage.BucketLifecycleRuleActionArgs(type="Delete"),
                    condition=gcp.storage.BucketLifecycleRuleConditionArgs(age=RETENTION_DAYS),
                )
            ],
            force_destroy=False,
            opts=self._o(),
        )
        gcp.storage.BucketIAMMember(
            "site-requests-create",
            bucket=self.bucket.name,
            role="roles/storage.objectCreator",
            member=self.sa.member,
            opts=self._o(),
        )

    def alerts(self) -> None:
        channel = gcp.monitoring.NotificationChannel(
            "site-founder",
            project=self.pid,
            display_name="Founder email (pilot requests)",
            type="email",
            labels={"email_address": self.cfg.notify_email},
            opts=self._o(),
        )
        for name, event, title in (
            ("site-request-stored", STORED_EVENT, "A pilot request came in on delimitus.com"),
            ("site-request-failed", FAILED_EVENT, "A delimitus.com pilot request was not stored"),
        ):
            gcp.monitoring.AlertPolicy(
                name,
                project=self.pid,
                display_name=title,
                combiner="OR",
                severity="WARNING" if event == FAILED_EVENT else None,
                conditions=[
                    gcp.monitoring.AlertPolicyConditionArgs(
                        display_name=event,
                        condition_matched_log=gcp.monitoring.AlertPolicyConditionConditionMatchedLogArgs(
                            filter=log_filter(event)
                        ),
                    )
                ],
                alert_strategy=gcp.monitoring.AlertPolicyAlertStrategyArgs(
                    notification_rate_limit=gcp.monitoring.AlertPolicyAlertStrategyNotificationRateLimitArgs(
                        period="300s"
                    ),
                    auto_close="1800s",
                ),
                documentation=gcp.monitoring.AlertPolicyDocumentationArgs(
                    content=(
                        f"Read the request in gs://{BUCKET}/requests/ (the log line carries no "
                        "personal data). Runbook: landing/README.md."
                    ),
                    mime_type="text/markdown",
                ),
                notification_channels=[channel.name],
                opts=self._o(),
            )

    def service(self, image: str) -> gcp.cloudrunv2.Service:
        env = {
            "SSC_LANDING_BUCKET": self.bucket.name,
            "SSC_LANDING_ORIGIN": ORIGIN,
            "SSC_LANDING_REDIRECT_HOSTS": WWW,
            "SSC_LANDING_TRUSTED_HOPS": "2",
        }
        return gcp.cloudrunv2.Service(
            "site-service",
            project=self.pid,
            name=SERVICE,
            location=n.REGION,
            # Reached only through the load balancer, never at its run.app address.
            ingress="INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
            # A public page: no Google identity is asked of visitors, and no allUsers binding.
            invoker_iam_disabled=True,
            deletion_protection=True,
            scaling=gcp.cloudrunv2.ServiceScalingArgs(max_instance_count=MAX_INSTANCES),
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=self.sa.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0),
                max_instance_request_concurrency=80,
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=image,
                        envs=[
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name=k, value=v)
                            for k, v in sorted(env.items())
                        ],
                        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                            cpu_idle=True, limits={"cpu": "1", "memory": "512Mi"}
                        ),
                    )
                ],
            ),
            opts=self._o(),
        )

    def load_balancer(self, service: gcp.cloudrunv2.Service, dns_project: str) -> None:
        o = self._o()
        address = gcp.compute.GlobalAddress("site-ip", project=self.pid, name="site", opts=o)
        neg = gcp.compute.RegionNetworkEndpointGroup(
            "site-neg",
            project=self.pid,
            name="site",
            region=n.REGION,
            network_endpoint_type="SERVERLESS",
            cloud_run=gcp.compute.RegionNetworkEndpointGroupCloudRunArgs(service=service.name),
            opts=o,
        )
        backend = gcp.compute.BackendService(
            "site-backend",
            project=self.pid,
            name="site",
            load_balancing_scheme="EXTERNAL_MANAGED",
            protocol="HTTPS",
            backends=[gcp.compute.BackendServiceBackendArgs(group=neg.id)],
            log_config=gcp.compute.BackendServiceLogConfigArgs(enable=False),
            opts=o,
        )
        https_map = gcp.compute.URLMap(
            "site-urlmap",
            project=self.pid,
            name="site",
            default_service=backend.id,
            host_rules=[gcp.compute.URLMapHostRuleArgs(hosts=[WWW], path_matcher="www")],
            path_matchers=[
                gcp.compute.URLMapPathMatcherArgs(
                    name="www",
                    default_url_redirect=gcp.compute.URLMapPathMatcherDefaultUrlRedirectArgs(
                        host_redirect=APEX,
                        https_redirect=True,
                        redirect_response_code="MOVED_PERMANENTLY_DEFAULT",
                        strip_query=False,
                    ),
                )
            ],
            opts=o,
        )
        cert = gcp.compute.ManagedSslCertificate(
            "site-cert",
            project=self.pid,
            name="site",
            managed=gcp.compute.ManagedSslCertificateManagedArgs(domains=[f"{APEX}.", f"{WWW}."]),
            opts=o,
        )
        tls = gcp.compute.SSLPolicy(
            "site-tls",
            project=self.pid,
            name="site",
            profile="MODERN",
            min_tls_version="TLS_1_2",
            opts=o,
        )
        https_proxy = gcp.compute.TargetHttpsProxy(
            "site-https",
            project=self.pid,
            name="site",
            url_map=https_map.id,
            ssl_certificates=[cert.id],
            ssl_policy=tls.id,
            opts=o,
        )
        gcp.compute.GlobalForwardingRule(
            "site-443",
            project=self.pid,
            name="site-https",
            load_balancing_scheme="EXTERNAL_MANAGED",
            ip_address=address.address,
            port_range="443",
            target=https_proxy.id,
            opts=o,
        )
        http_map = gcp.compute.URLMap(
            "site-urlmap-http",
            project=self.pid,
            name="site-http",
            default_url_redirect=gcp.compute.URLMapDefaultUrlRedirectArgs(
                https_redirect=True,
                redirect_response_code="MOVED_PERMANENTLY_DEFAULT",
                strip_query=False,
            ),
            opts=o,
        )
        http_proxy = gcp.compute.TargetHttpProxy(
            "site-http", project=self.pid, name="site-http", url_map=http_map.id, opts=o
        )
        gcp.compute.GlobalForwardingRule(
            "site-80",
            project=self.pid,
            name="site-http",
            load_balancing_scheme="EXTERNAL_MANAGED",
            ip_address=address.address,
            port_range="80",
            target=http_proxy.id,
            opts=o,
        )
        for name, host in (("site-a-apex", APEX), ("site-a-www", WWW)):
            gcp.dns.RecordSet(
                name,
                project=dns_project,
                managed_zone=self.cfg.dns_zone,
                name=f"{host}.",
                type="A",
                ttl=300,
                rrdatas=[address.address],
                opts=self.opts,
            )
        pulumi.export("landing_ip", address.address)


def build(folder_id: pulumi.Input[str], opts: pulumi.ResourceOptions) -> None:
    cfg = landing_config(pulumi.Config())
    if not cfg.enabled:
        return
    site = Landing(cfg, folder_id, opts)
    site.base()
    site.alerts()
    pulumi.export("landing_registry", f"{n.REGION}-docker.pkg.dev/{PROJECT}/{REPOSITORY}")
    pulumi.export("landing_bucket", BUCKET)
    if cfg.image is not None and cfg.dns_project is not None:
        site.load_balancer(site.service(cfg.image), cfg.dns_project)
