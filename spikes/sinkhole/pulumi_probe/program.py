"""The probe's resources: one response policy with no network, and N rules shaped like the cell's
``*.<tld>.`` sinkhole rules (``Cell.dns_policy`` in ``infra/ssc_infra/cell.py``). It copies that
code's resource arguments and does not import ``ssc_infra``.

What is the same as the cell: the rule's arguments (policy name taken from the policy's output,
``local_data`` of one CNAME to the sinkhole's name with ttl 300), the explicit provider (quota and
billing to ``ssc-platform-0``, as ``platform.provider`` does) and each rule depending on the
sinkhole rule. What is not: the project is a plain string (the cell's is an output), the rules
depend on no API-enabling resources (the cell's depend on about 20), and no VPC is attached.
"""

from typing import Final

import pulumi
import pulumi_gcp as gcp

PROJECT: Final = "ssc-platform-0"
FORBIDDEN_PROJECT: Final = "ristretto-506621"
REGION: Final = "us-central1"
POLICY: Final = "ssc-exp091p"
SINKHOLE: Final = "192.0.2.1"  # copied from cell.SINKHOLE
SINKHOLE_V6: Final = "100::1"  # copied from cell.SINKHOLE_V6
SINKHOLE_NAME: Final = "sinkhole.ssc-cell."  # copied from cell.SINKHOLE_NAME
DEFAULT_COUNT: Final = 100


def check_project(project: str) -> str:
    if project != PROJECT:
        raise ValueError(f"the probe only touches {PROJECT}, not {project!r}")
    return project


def rule_name(i: int) -> str:
    return f"{POLICY}-{i}"


def build(project: str, count: int) -> None:
    check_project(project)
    if count < 1:
        raise ValueError("count must be at least 1")
    provider = gcp.Provider(
        "gcp",
        project=project,
        region=REGION,
        billing_project=PROJECT,
        user_project_override=True,
        default_labels={"ssc-managed": "pulumi"},
    )
    opts = pulumi.ResourceOptions(provider=provider)
    policy = gcp.dns.ResponsePolicy(
        "policy",
        project=project,
        response_policy_name=POLICY,
        description="SSC-091 probe: a throwaway policy with no network.",
        opts=opts,
    )
    sink = gcp.dns.ResponsePolicyRule(
        f"{POLICY}-sink",
        project=project,
        response_policy=policy.response_policy_name,
        rule_name=f"{POLICY}-sink",
        dns_name=SINKHOLE_NAME,
        local_data=gcp.dns.ResponsePolicyRuleLocalDataArgs(
            local_datas=[
                gcp.dns.ResponsePolicyRuleLocalDataLocalDataArgs(
                    name=SINKHOLE_NAME, type=kind, ttl=300, rrdatas=[address]
                )
                for kind, address in (("A", SINKHOLE), ("AAAA", SINKHOLE_V6))
            ]
        ),
        opts=opts,
    )
    for i in range(count):
        name = rule_name(i)
        gcp.dns.ResponsePolicyRule(
            name,
            project=project,
            response_policy=policy.response_policy_name,
            rule_name=name,
            dns_name=f"*.{name}.",
            local_data=gcp.dns.ResponsePolicyRuleLocalDataArgs(
                local_datas=[
                    gcp.dns.ResponsePolicyRuleLocalDataLocalDataArgs(
                        name=f"*.{name}.", type="CNAME", ttl=300, rrdatas=[SINKHOLE_NAME]
                    )
                ]
            ),
            opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=[sink])),
        )
    pulumi.export("rules", count)
