"""delimitus.com's page and pilot request form (SSC-065, decision 028): ``python -m
ssc_landing`` on Cloud Run in the public stage's control project, behind the control plane's own
entry load balancer (``control.py`` wires the hosts, the certificate and the records).

Built only when the control setting ``landing`` is true, in two steps, so nothing is public
before an image exists:

- ``landing: true``: the service's account, the request bucket and its one binding, the founder
  alert and the monitoring API it needs.
- ``landing_image: <platform registry>/...@sha256:...`` as well: the Cloud Run service and its
  backend service, which ``control.py`` puts on the entry's URL map.

The service's account holds one role, on one bucket: create objects. It cannot read, list or
replace a stored request, and it has nothing on the control database, a secret or any other
bucket.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import pulumi
import pulumi_gcp as gcp

from ssc_infra import naming as n

SERVICE: Final = "ssc-landing"
APEX: Final = n.LANDING_HOSTS[0]
WWW: Final = n.LANDING_HOSTS[1]
ORIGIN: Final = f"https://{APEX}"
RETENTION_DAYS: Final = 365
"""The page says 12 months; a request is deleted on the next lifecycle pass after a year."""
MAX_INSTANCES: Final = 3
"""The form allows 5 requests a minute per address in each instance."""
LIMITS: Final = {"cpu": "1", "memory": "512Mi"}
STORED_EVENT: Final = "pilot_request_stored"
FAILED_EVENT: Final = "pilot_request_not_stored"
MONITORING_API: Final = "monitoring.googleapis.com"


@dataclass(frozen=True, slots=True)
class LandingSettings:
    """``image`` is None until the first image is pushed; ``notify_email`` is the founder's."""

    image: str | None
    notify_email: str


def log_filter(event: str) -> str:
    return (
        'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{SERVICE}" '
        f'AND jsonPayload.event="{event}"'
    )


class Landing:
    """The landing resources of one control project. ``backend`` is the backend service that
    ``control.py`` puts behind the apex and ``www``; None until the image is set."""

    def __init__(
        self,
        pid: pulumi.Input[str],
        stage: n.Stage,
        settings: LandingSettings,
        name: Callable[[str], str],
        opts: pulumi.ResourceOptions,
    ) -> None:
        self.pid = pid
        self.stage: n.Stage = stage
        self.settings = settings
        self.name = name
        self.opts = opts
        self.bucket_name = n.control_bucket(stage, n.LANDING_BUCKET_PURPOSE)
        self.backend: gcp.compute.BackendService | None = None
        # Not ``control-<stage>-monitoring``: ``platform._alerts`` owns that name when
        # ``oncall_email`` is set, and enabling an API twice is harmless.
        self.monitoring = gcp.projects.Service(
            name("landing-monitoring"),
            project=pid,
            service=MONITORING_API,
            disable_on_destroy=False,
            opts=opts,
        )
        self.sa = gcp.serviceaccount.Account(
            name("landing-sa"),
            project=pid,
            account_id=n.LANDING_SA,
            display_name="delimitus.com: creates pilot requests, reads nothing",
            opts=opts,
        )
        self.bucket = self._bucket()
        self._alerts()

    def _bucket(self) -> gcp.storage.Bucket:
        bucket = gcp.storage.Bucket(
            self.name("landing-requests"),
            project=self.pid,
            name=self.bucket_name,
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
            opts=self.opts,
        )
        gcp.storage.BucketIAMMember(
            self.name("landing-requests-create"),
            bucket=bucket.name,
            role="roles/storage.objectCreator",
            member=self.sa.member,
            opts=self.opts,
        )
        return bucket

    def _alerts(self) -> None:
        after = pulumi.ResourceOptions.merge(
            self.opts, pulumi.ResourceOptions(depends_on=[self.monitoring])
        )
        channel = gcp.monitoring.NotificationChannel(
            self.name("landing-founder"),
            project=self.pid,
            display_name="Founder email (pilot requests)",
            type="email",
            labels={"email_address": self.settings.notify_email},
            opts=after,
        )
        for part, event, title in (
            ("request-stored", STORED_EVENT, "A pilot request came in on delimitus.com"),
            ("request-failed", FAILED_EVENT, "A delimitus.com pilot request was not stored"),
        ):
            gcp.monitoring.AlertPolicy(
                self.name(f"landing-{part}"),
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
                        f"Read the request in gs://{self.bucket_name}/requests/ (the log line "
                        "carries no personal data). Runbook: landing/README.md."
                    ),
                    mime_type="text/markdown",
                ),
                notification_channels=[channel.name],
                opts=after,
            )

    def serve(self, image: str) -> gcp.compute.BackendService:
        """The Cloud Run service, open to every caller at the load balancer's door, and the
        backend service on it."""
        env = {
            "SSC_LANDING_BUCKET": self.bucket.name,
            "SSC_LANDING_ORIGIN": ORIGIN,
            "SSC_LANDING_REDIRECT_HOSTS": WWW,
            "SSC_LANDING_TRUSTED_HOPS": "2",
        }
        service = gcp.cloudrunv2.Service(
            self.name(SERVICE),
            project=self.pid,
            name=SERVICE,
            location=n.REGION,
            # Reached only through the load balancer, never at its run.app address.
            ingress="INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
            deletion_protection=self.stage == "prod",
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
                            cpu_idle=True, limits=LIMITS
                        ),
                    )
                ],
            ),
            opts=self.opts,
        )
        # A public page: every visitor may call it, as the control hosts' services are.
        gcp.cloudrunv2.ServiceIamMember(
            self.name(f"{SERVICE}-invoker"),
            project=self.pid,
            location=n.REGION,
            name=service.name,
            role="roles/run.invoker",
            member="allUsers",
            opts=self.opts,
        )
        neg = gcp.compute.RegionNetworkEndpointGroup(
            self.name("landing-neg"),
            project=self.pid,
            name=SERVICE,
            region=n.REGION,
            network_endpoint_type="SERVERLESS",
            cloud_run=gcp.compute.RegionNetworkEndpointGroupCloudRunArgs(service=service.name),
            opts=self.opts,
        )
        self.backend = gcp.compute.BackendService(
            self.name("landing-backend"),
            project=self.pid,
            name=SERVICE,
            load_balancing_scheme="EXTERNAL_MANAGED",
            protocol="HTTPS",
            backends=[gcp.compute.BackendServiceBackendArgs(group=neg.id)],
            log_config=gcp.compute.BackendServiceLogConfigArgs(enable=False),
            opts=self.opts,
        )
        return self.backend


def build(
    pid: pulumi.Input[str],
    stage: n.Stage,
    settings: LandingSettings,
    name: Callable[[str], str],
    opts: pulumi.ResourceOptions,
) -> Landing:
    """Everything of the landing page in one control project; ``control.py`` calls it for the
    public stage and wires the returned backend into the entry load balancer."""
    site = Landing(pid, stage, settings, name, opts)
    if settings.image is not None:
        site.serve(settings.image)
    pulumi.export("landing_bucket", site.bucket.name)
    return site
