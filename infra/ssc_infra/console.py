"""console.delimitus.com (SSC gap 1): ``python -m ssc_console_host`` serving the console's
production build on Cloud Run in the public stage's control project, behind the control plane's
own entry load balancer (``control.py`` wires the host, its path matcher, the certificate and
the record).

Built only when the control setting ``console_image`` is set; nothing is declared before an
image exists. The host serves static files and calls nothing, so its account holds no role: it
exists so the service does not run as the project's default compute account.
"""

from collections.abc import Callable
from typing import Final

import pulumi
import pulumi_gcp as gcp

from ssc_infra import naming as n

SERVICE: Final = "ssc-console"
AUTH_ORIGIN: Final = n.origin(n.AUTH_HOST)
"""The auth host the console's build signs in through; its content security policy names it."""
MAX_INSTANCES: Final = 3
LIMITS: Final = {"cpu": "1", "memory": "512Mi"}


class Console:
    """The console host of one control project. ``backend`` is the backend service that
    ``control.py`` makes the default of the console host's path matcher."""

    def __init__(
        self,
        pid: pulumi.Input[str],
        stage: n.Stage,
        image: str,
        name: Callable[[str], str],
        opts: pulumi.ResourceOptions,
    ) -> None:
        self.sa = gcp.serviceaccount.Account(
            name("console-sa"),
            project=pid,
            account_id=n.CONSOLE_SA,
            display_name="console.delimitus.com: serves the console's files, holds no role",
            opts=opts,
        )
        service = gcp.cloudrunv2.Service(
            name(SERVICE),
            project=pid,
            name=SERVICE,
            location=n.REGION,
            # Reached only through the load balancer, never at its run.app address.
            ingress="INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
            deletion_protection=stage == "prod",
            scaling=gcp.cloudrunv2.ServiceScalingArgs(max_instance_count=MAX_INSTANCES),
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=self.sa.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0),
                max_instance_request_concurrency=80,
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=image,
                        envs=[
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name="SSC_CONSOLE_AUTH_ORIGIN", value=AUTH_ORIGIN
                            )
                        ],
                        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                            cpu_idle=True, limits=LIMITS
                        ),
                    )
                ],
            ),
            opts=opts,
        )
        # Public files: every visitor may load them, as the other entry hosts' services are.
        gcp.cloudrunv2.ServiceIamMember(
            name(f"{SERVICE}-invoker"),
            project=pid,
            location=n.REGION,
            name=service.name,
            role="roles/run.invoker",
            member="allUsers",
            opts=opts,
        )
        neg = gcp.compute.RegionNetworkEndpointGroup(
            name("console-neg"),
            project=pid,
            name=SERVICE,
            region=n.REGION,
            network_endpoint_type="SERVERLESS",
            cloud_run=gcp.compute.RegionNetworkEndpointGroupCloudRunArgs(service=service.name),
            opts=opts,
        )
        self.backend = gcp.compute.BackendService(
            name("console-backend"),
            project=pid,
            name=SERVICE,
            load_balancing_scheme="EXTERNAL_MANAGED",
            protocol="HTTPS",
            backends=[gcp.compute.BackendServiceBackendArgs(group=neg.id)],
            log_config=gcp.compute.BackendServiceLogConfigArgs(enable=False),
            opts=opts,
        )


def build(
    pid: pulumi.Input[str],
    stage: n.Stage,
    image: str,
    name: Callable[[str], str],
    opts: pulumi.ResourceOptions,
) -> Console:
    """The console host in one control project; ``control.py`` calls it for the public stage
    when ``console_image`` is set and wires the returned backend into the entry load balancer."""
    return Console(pid, stage, image, name, opts)
