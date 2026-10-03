"""The ``platform`` stack: folders, folder policies, the staging control identity, the secret-read
deny rule, just-in-time staff access, the budget (decisions 021 and 022), the public DNS zones
(SSC-088), the cells' organisation policies with the gateway's public-invoker tag (SSC-095) and
the cell deployer that turns on a cell's lazy resources (SSC-087).

The ``ssc-platform`` folder and the ``ssc-platform-0`` project that holds this program's state are
made by ``python -m ssc_infra.bootstrap`` first; this stack takes the folder's ID from config.
"""

import json
from collections.abc import Sequence

import pulumi
import pulumi_gcp as gcp

from ssc_infra import naming as n
from ssc_infra import policies
from ssc_infra.policies import LOCATIONS, PUBLIC_TAG_KEY, PUBLIC_TAG_VALUE, Rule

BUDGET_USD = 250
BUDGET_THRESHOLDS = (
    (0.5, "CURRENT_SPEND"),
    (0.9, "CURRENT_SPEND"),
    (1.0, "CURRENT_SPEND"),
    (1.0, "FORECASTED_SPEND"),
)
TAG_USER = "roles/resourcemanager.tagUser"
JIT_ROLE = "roles/writer"
JIT_MAX = "3600s"
PAM_AGENT = f"serviceAccount:service-org-{n.ORG_ID}@gcp-sa-pam.iam.gserviceaccount.com"
PAM_AGENT_ROLE = "roles/privilegedaccessmanager.folderServiceAgent"
ZONE_RECORD_PERMISSIONS = (
    "dns.changes.create",
    "dns.changes.get",
    "dns.managedZones.get",
    "dns.resourceRecordSets.create",
    "dns.resourceRecordSets.delete",
    "dns.resourceRecordSets.get",
    "dns.resourceRecordSets.list",
    "dns.resourceRecordSets.update",
)
DEPLOYER_FOLDER_ROLES = (
    "roles/cloudsql.admin",
    "roles/compute.instanceAdmin.v1",
    "roles/compute.networkUser",
    "roles/iam.serviceAccountUser",
    "roles/resourcemanager.tagUser",
    "roles/run.admin",
)
DEPLOYER_TIMEOUT = "3600s"


def provider() -> gcp.Provider:
    """Quota and billing go to ``ssc-platform-0``, never to the caller's default project."""
    return gcp.Provider(
        "gcp",
        region=n.REGION,
        billing_project=n.BOOTSTRAP_PROJECT,
        user_project_override=True,
        default_labels={"ssc-managed": "pulumi"},
    )


def folder_ref(folder_id: pulumi.Input[str]) -> pulumi.Output[str]:
    return pulumi.Output.concat("folders/", folder_id)


def _log_location(name: str, folder_id: pulumi.Input[str], opts: pulumi.ResourceOptions) -> None:
    gcp.logging.FolderSettings(
        f"{name}-logs", folder=folder_id, storage_location=n.REGION, opts=opts
    )


def _location_policy(name: str, folder_id: pulumi.Input[str], opts: pulumi.ResourceOptions) -> None:
    ref = folder_ref(folder_id)
    gcp.orgpolicy.Policy(
        f"{name}-locations",
        name=pulumi.Output.concat(ref, "/policies/gcp.resourceLocations"),
        parent=ref,
        spec=gcp.orgpolicy.PolicySpecArgs(
            rules=[
                gcp.orgpolicy.PolicySpecRuleArgs(
                    values=gcp.orgpolicy.PolicySpecRuleValuesArgs(allowed_values=[LOCATIONS])
                )
            ]
        ),
        opts=opts,
    )


def _public_tag(
    binders: Sequence[pulumi.Input[str]], opts: pulumi.ResourceOptions
) -> tuple[pulumi.Output[str], pulumi.Output[str]]:
    """The tag that marks the one resource allowed a public member: each cell's gateway service.
    Only ``binders`` (the operator and the cell deployer) may bind it; the grant is authoritative,
    so another holder added by hand is removed by the next run. Returns the tag key and value
    IDs."""
    key = gcp.tags.TagKey(
        "public-invoker-key",
        parent=f"organizations/{n.ORG_ID}",
        short_name=PUBLIC_TAG_KEY,
        description="Bound only to a cell's gateway service: lifts domain-restricted sharing.",
        opts=opts,
    )
    key_id = pulumi.Output.concat("tagKeys/", key.name)
    value = gcp.tags.TagValue(
        "public-invoker-gateway",
        parent=key_id,
        short_name=PUBLIC_TAG_VALUE,
        description="The cell gateway's allUsers invoker (SSC-088, SSC-095).",
        opts=opts,
    )
    value_id = pulumi.Output.concat("tagValues/", value.name)
    gcp.tags.TagValueIamBinding(
        "public-invoker-binders",
        tag_value=value_id,
        role=TAG_USER,
        members=list(binders),
        opts=opts,
    )
    return key_id, value_id


def _spec_rules(
    rule: Rule, tag: tuple[pulumi.Output[str], pulumi.Output[str]]
) -> list[gcp.orgpolicy.PolicySpecRuleArgs]:
    """A list constraint takes values or deny-all; a boolean or managed one ``enforce``, with
    the managed one's parameters. The tag exception is a second rule, off where the tag is."""
    if rule.deny_all:
        main = gcp.orgpolicy.PolicySpecRuleArgs(deny_all="TRUE")
    elif rule.allowed:
        main = gcp.orgpolicy.PolicySpecRuleArgs(
            values=gcp.orgpolicy.PolicySpecRuleValuesArgs(allowed_values=list(rule.allowed))
        )
    else:
        params = {k: list(v) for k, v in rule.parameters.items()}
        main = gcp.orgpolicy.PolicySpecRuleArgs(
            enforce="TRUE", parameters=json.dumps(params) if params else None
        )
    if not rule.tag_exception:
        return [main]
    key_id, value_id = tag
    exception = gcp.orgpolicy.PolicySpecRuleArgs(
        enforce="FALSE",
        condition=gcp.orgpolicy.PolicySpecRuleConditionArgs(
            title="the cell gateway's public invoker",
            expression=pulumi.Output.format("resource.matchTagId('{0}', '{1}')", key_id, value_id),
        ),
    )
    return [main, exception]


def _cell_policies(
    folder_id: pulumi.Input[str],
    rules: Sequence[Rule],
    tag: tuple[pulumi.Output[str], pulumi.Output[str]],
    opts: pulumi.ResourceOptions,
) -> None:
    ref = folder_ref(folder_id)
    for rule in rules:
        gcp.orgpolicy.Policy(
            f"{n.CELLS_FOLDER}-{rule.key}",
            name=pulumi.Output.concat(ref, "/policies/", rule.constraint),
            parent=ref,
            spec=gcp.orgpolicy.PolicySpecArgs(rules=_spec_rules(rule, tag)),
            opts=opts,
        )


def _folder(
    name: str, display: str, parent: pulumi.Input[str], opts: pulumi.ResourceOptions
) -> gcp.organizations.Folder:
    folder = gcp.organizations.Folder(
        name, display_name=display, parent=parent, deletion_protection=True, opts=opts
    )
    _log_location(name, folder.folder_id, opts)
    return folder


def deny_attachment(folder_id: pulumi.Input[str]) -> pulumi.Output[str]:
    return pulumi.Output.concat("cloudresourcemanager.googleapis.com%2Ffolders%2F", folder_id)


def sa_principal(email: pulumi.Input[str]) -> pulumi.Output[str]:
    return pulumi.Output.concat("principal://iam.googleapis.com/projects/-/serviceAccounts/", email)


def _nightly(
    project: pulumi.Input[str],
    control: gcp.serviceaccount.Account,
    opts: pulumi.ResourceOptions,
) -> gcp.serviceaccount.Account:
    """The nightly probe run (SSC-017): GitHub Actions on ``main`` of this repository, running
    the nightly workflow, becomes ``ssc-nightly`` with no key. It may mint ID tokens as the
    control plane, which is all it needs to call a cell's agent; each probe cell grants it the
    probe job and read access to that job's results. Logging bills a log read to the caller's
    project, so this project needs the Logging API to read a cell's job results."""
    apis = [
        gcp.projects.Service(
            f"control-staging-{api.split('.')[0]}",
            project=project,
            service=api,
            disable_on_destroy=False,
            opts=opts,
        )
        for api in ("sts.googleapis.com", "iamcredentials.googleapis.com", "logging.googleapis.com")
    ]
    after = pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=apis))
    pool = gcp.iam.WorkloadIdentityPool(
        "github-pool",
        project=project,
        workload_identity_pool_id="ssc-github",
        display_name="SSC GitHub Actions",
        opts=after,
    )
    workflow = f"{n.GITHUB_REPOSITORY}/{n.NIGHTLY_WORKFLOW}@refs/heads/main"
    gcp.iam.WorkloadIdentityPoolProvider(
        "github-provider",
        project=project,
        workload_identity_pool_id=pool.workload_identity_pool_id,
        workload_identity_pool_provider_id="github",
        display_name="GitHub Actions OIDC",
        attribute_mapping={
            "google.subject": "assertion.sub",
            "attribute.repository": "assertion.repository",
            "attribute.workflow_ref": "assertion.workflow_ref",
        },
        attribute_condition=(
            f'assertion.repository == "{n.GITHUB_REPOSITORY}" '
            f'&& assertion.workflow_ref == "{workflow}"'
        ),
        oidc=gcp.iam.WorkloadIdentityPoolProviderOidcArgs(
            issuer_uri="https://token.actions.githubusercontent.com"
        ),
        opts=after,
    )
    nightly = gcp.serviceaccount.Account(
        "nightly-sa",
        project=project,
        account_id=n.NIGHTLY_SA,
        display_name="SSC nightly probe run",
        opts=after,
    )
    gcp.serviceaccount.IAMMember(
        "nightly-wif",
        service_account_id=nightly.name,
        role="roles/iam.workloadIdentityUser",
        member=pulumi.Output.concat(
            "principalSet://iam.googleapis.com/",
            pool.name,
            f"/attribute.repository/{n.GITHUB_REPOSITORY}",
        ),
        opts=opts,
    )
    gcp.serviceaccount.IAMMember(
        "nightly-control-id-tokens",
        service_account_id=control.name,
        role="roles/iam.serviceAccountOpenIdTokenCreator",
        member=nightly.member,
        opts=opts,
    )
    return nightly


def _zone(name: str, domain: str, opts: pulumi.ResourceOptions) -> gcp.dns.ManagedZone:
    """A public zone in ``ssc-platform-0``; the founder points the registrar at its name
    servers. DNSSEC is off until the registrar's old DS records are gone."""
    return gcp.dns.ManagedZone(
        f"zone-{name}",
        project=n.BOOTSTRAP_PROJECT,
        name=name,
        dns_name=f"{domain}.",
        description=f"SSC public names under {domain}",
        visibility="public",
        dnssec_config=gcp.dns.ManagedZoneDnssecConfigArgs(state="off"),
        opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(protect=True)),
    )


def _zones(
    writers: dict[str, pulumi.Input[str]], opts: pulumi.ResourceOptions
) -> dict[str, gcp.dns.ManagedZone]:
    """The apps zone, where each cell stack writes its own wildcard and certificate
    authorisation records, and the platform zone for ``api``, ``auth`` and ``keys``. A cell stack
    runs as the operator or as the cell deployer (SSC-087); ``writers`` names both."""
    apps = _zone(n.APPS_ZONE, n.APPS_DOMAIN, opts)
    platform_hosts = _zone(n.PLATFORM_ZONE, n.PLATFORM_DOMAIN, opts)
    writer = gcp.projects.IAMCustomRole(
        "apps-zone-records",
        project=n.BOOTSTRAP_PROJECT,
        role_id="sscZoneRecords",
        title="SSC: write records in one zone",
        description="Granted on a zone, never on the project.",
        permissions=list(ZONE_RECORD_PERMISSIONS),
        opts=opts,
    )
    for name, member in writers.items():
        gcp.dns.DnsManagedZoneIamMember(
            name,
            project=n.BOOTSTRAP_PROJECT,
            managed_zone=apps.name,
            role=writer.name,
            member=member,
            opts=opts,
        )
    return {"apps": apps, "platform": platform_hosts}


def _deployer(opts: pulumi.ResourceOptions) -> gcp.serviceaccount.Account:
    """The cell deployer's identity (SSC-087), with the state it applies cell stacks from: the
    state bucket, the key that wraps its secrets, and quota in this project."""
    deployer = gcp.serviceaccount.Account(
        "deployer-sa",
        project=n.BOOTSTRAP_PROJECT,
        account_id=n.DEPLOYER,
        display_name="SSC cell deployer (lazy resources only)",
        opts=opts,
    )
    gcp.storage.BucketIAMMember(
        "deployer-state",
        bucket=n.STATE_BUCKET,
        role="roles/storage.objectAdmin",
        member=deployer.member,
        opts=opts,
    )
    gcp.kms.CryptoKeyIAMMember(
        "deployer-state-key",
        crypto_key_id=n.SECRETS_PROVIDER.removeprefix("gcpkms://"),
        role="roles/cloudkms.cryptoKeyEncrypterDecrypter",
        member=deployer.member,
        opts=opts,
    )
    gcp.projects.IAMMember(
        "deployer-quota",
        project=n.BOOTSTRAP_PROJECT,
        role="roles/serviceusage.serviceUsageConsumer",
        member=deployer.member,
        opts=opts,
    )
    return deployer


def _deployer_cells(
    deployer: gcp.serviceaccount.Account,
    cells: gcp.organizations.Folder,
    opts: pulumi.ResourceOptions,
) -> None:
    """What applying a cell stack with a lazy flag on needs: the database, the proxy group, the
    data gateway, and the tag and records the stack keeps on its gateway. No billing, deny-rule
    or secret-value role, so the deployer can neither make a cell nor weaken one."""
    for role in DEPLOYER_FOLDER_ROLES:
        gcp.folder.IAMMember(
            f"deployer-{role.removeprefix('roles/')}",
            folder=cells.name,
            role=role,
            member=deployer.member,
            opts=opts,
        )


def _deployer_job(
    deployer: gcp.serviceaccount.Account,
    control: gcp.serviceaccount.Account,
    image: str | None,
    opts: pulumi.ResourceOptions,
) -> None:
    """The job runs ``infra/deployer/Dockerfile`` once ``deployer_image`` names a build of it in
    this project's registry. The control plane may start it, with its own arguments, and read
    its executions; nothing else."""
    gcp.artifactregistry.Repository(
        "platform-registry",
        project=n.BOOTSTRAP_PROJECT,
        location=n.REGION,
        repository_id=n.PLATFORM_REPOSITORY,
        format="DOCKER",
        description="SSC's own images in the platform project: the cell deployer, the build "
        "tools image and the Railpack frontend mirror.",
        opts=opts,
    )
    if image is None:
        return
    job = gcp.cloudrunv2.Job(
        "cell-deployer",
        project=n.BOOTSTRAP_PROJECT,
        name=n.DEPLOYER,
        location=n.REGION,
        deletion_protection=False,
        template=gcp.cloudrunv2.JobTemplateArgs(
            task_count=1,
            template=gcp.cloudrunv2.JobTemplateTemplateArgs(
                service_account=deployer.email,
                max_retries=0,
                timeout=DEPLOYER_TIMEOUT,
                containers=[
                    gcp.cloudrunv2.JobTemplateTemplateContainerArgs(
                        image=image,
                        resources=gcp.cloudrunv2.JobTemplateTemplateContainerResourcesArgs(
                            limits={"cpu": "2", "memory": "2Gi"}
                        ),
                    )
                ],
            ),
        ),
        opts=opts,
    )
    for name, role in (
        ("runs", "roles/run.jobsExecutorWithOverrides"),
        ("reads", "roles/run.viewer"),
    ):
        gcp.cloudrunv2.JobIamMember(
            f"cell-deployer-control-{name}",
            project=n.BOOTSTRAP_PROJECT,
            location=n.REGION,
            name=job.name,
            role=role,
            member=control.member,
            opts=opts,
        )


def build() -> None:
    config = pulumi.Config()
    platform_folder = config.require("platform_folder_id")
    operator = config.get("operator") or n.OPERATOR
    opts = pulumi.ResourceOptions(provider=provider())
    org = f"organizations/{n.ORG_ID}"

    _location_policy(n.PLATFORM_FOLDER, platform_folder, opts)

    cells = _folder(n.CELLS_FOLDER, n.CELLS_FOLDER, org, opts)
    deployer = _deployer(opts)
    _deployer_cells(deployer, cells, opts)
    rules = policies.cell_rules(operator, config.get_object("peering_allowed"))
    tag = _public_tag([operator, deployer.member], opts)
    _cell_policies(cells.folder_id, rules, tag, opts)
    stages = {
        stage: _folder(f"{n.CELLS_FOLDER}-{stage}", stage, cells.name, opts) for stage in n.STAGES
    }
    sandbox = _folder(n.SANDBOX_FOLDER, n.SANDBOX_FOLDER, org, opts)

    control_project = gcp.organizations.Project(
        "control-staging",
        project_id=n.control_project("staging"),
        name=n.control_project("staging"),
        folder_id=platform_folder,
        billing_account=n.BILLING_ACCOUNT,
        auto_create_network=False,
        deletion_policy="PREVENT",
        opts=opts,
    )
    iam_api = gcp.projects.Service(
        "control-staging-iam",
        project=control_project.project_id,
        service="iam.googleapis.com",
        disable_on_destroy=False,
        opts=opts,
    )
    control = gcp.serviceaccount.Account(
        "control-staging-sa",
        project=control_project.project_id,
        account_id=n.CONTROL_SA,
        display_name="SSC control plane (staging)",
        opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=[iam_api])),
    )

    nightly = _nightly(control_project.project_id, control, opts)
    zones = _zones(
        {"apps-zone-cell-deployer": operator, "apps-zone-deployer": deployer.member}, opts
    )
    _deployer_job(deployer, control, config.get("deployer_image"), opts)

    gcp.iam.DenyPolicy(
        "cells-secret-read",
        parent=deny_attachment(cells.folder_id),
        name="ssc-deny-secret-read",
        display_name="SSC identities never read secret values",
        rules=[
            gcp.iam.DenyPolicyRuleArgs(
                description="The control plane only adds versions; apps read their own.",
                deny_rule=gcp.iam.DenyPolicyRuleDenyRuleArgs(
                    denied_principals=[sa_principal(control.email)],
                    denied_permissions=[n.SECRET_READ],
                ),
            )
        ],
        opts=opts,
    )

    pam_agent = gcp.folder.IAMMember(
        "cells-pam-agent", folder=cells.name, role=PAM_AGENT_ROLE, member=PAM_AGENT, opts=opts
    )
    gcp.privilegedaccessmanager.Entitlement(
        "cells-jit",
        entitlement_id="ssc-cells-jit",
        location="global",
        parent=folder_ref(cells.folder_id),
        max_request_duration=JIT_MAX,
        eligible_users=[
            gcp.privilegedaccessmanager.EntitlementEligibleUserArgs(principals=[operator])
        ],
        privileged_access=gcp.privilegedaccessmanager.EntitlementPrivilegedAccessArgs(
            gcp_iam_access=gcp.privilegedaccessmanager.EntitlementPrivilegedAccessGcpIamAccessArgs(
                resource=pulumi.Output.concat(
                    "//cloudresourcemanager.googleapis.com/folders/", cells.folder_id
                ),
                resource_type="cloudresourcemanager.googleapis.com/Folder",
                role_bindings=[
                    gcp.privilegedaccessmanager.EntitlementPrivilegedAccessGcpIamAccessRoleBindingArgs(
                        role=JIT_ROLE
                    )
                ],
            )
        ),
        requester_justification_config=gcp.privilegedaccessmanager.EntitlementRequesterJustificationConfigArgs(
            unstructured=gcp.privilegedaccessmanager.EntitlementRequesterJustificationConfigUnstructuredArgs()
        ),
        opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=[pam_agent])),
    )

    gcp.serviceaccount.IAMMember(
        "control-staging-signs-urls",
        service_account_id=control.name,
        role="roles/iam.serviceAccountTokenCreator",
        member=control.member,
        opts=opts,
    )

    gcp.billing.Budget(
        "ssc-monthly",
        billing_account=n.BILLING_ACCOUNT,
        display_name=f"SSC ${BUDGET_USD} a month",
        amount=gcp.billing.BudgetAmountArgs(
            specified_amount=gcp.billing.BudgetAmountSpecifiedAmountArgs(
                currency_code="USD", units=str(BUDGET_USD)
            )
        ),
        budget_filter=gcp.billing.BudgetBudgetFilterArgs(
            calendar_period="MONTH",
            resource_ancestors=[
                folder_ref(platform_folder),
                folder_ref(cells.folder_id),
                folder_ref(sandbox.folder_id),
            ],
        ),
        threshold_rules=[
            gcp.billing.BudgetThresholdRuleArgs(threshold_percent=p, spend_basis=b)
            for p, b in BUDGET_THRESHOLDS
        ],
        opts=opts,
    )

    pulumi.export("platform_folder_id", platform_folder)
    pulumi.export("cells_folder_id", cells.folder_id)
    pulumi.export("stage_folder_ids", {s: f.folder_id for s, f in stages.items()})
    pulumi.export("sandbox_folder_id", sandbox.folder_id)
    pulumi.export("control_service_accounts", {"staging": control.email})
    pulumi.export("nightly_service_account", nightly.email)
    pulumi.export("apps_zone_name_servers", zones["apps"].name_servers)
    pulumi.export("platform_zone_name_servers", zones["platform"].name_servers)
    pulumi.export("public_invoker_tag", tag[1])
    pulumi.export("cell_policies", policies.summaries(rules))
    pulumi.export(
        "cell_deployer",
        {
            "service_account": deployer.email,
            "job": f"projects/{n.BOOTSTRAP_PROJECT}/locations/{n.REGION}/jobs/{n.DEPLOYER}",
        },
    )
