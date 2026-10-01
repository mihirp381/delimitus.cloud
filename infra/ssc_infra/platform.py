"""The ``platform`` stack: folders, folder policies, the staging control identity, the secret-read
deny rule, just-in-time staff access and the budget (decisions 021 and 022).

The ``ssc-platform`` folder and the ``ssc-platform-0`` project that holds this program's state are
made by ``python -m ssc_infra.bootstrap`` first; this stack takes the folder's ID from config.
"""

import pulumi
import pulumi_gcp as gcp

from ssc_infra import naming as n

LOCATIONS = "in:us-central1-locations"
BUDGET_USD = 250
BUDGET_THRESHOLDS = (
    (0.5, "CURRENT_SPEND"),
    (0.9, "CURRENT_SPEND"),
    (1.0, "CURRENT_SPEND"),
    (1.0, "FORECASTED_SPEND"),
)
JIT_ROLE = "roles/writer"
JIT_MAX = "3600s"
PAM_AGENT = f"serviceAccount:service-org-{n.ORG_ID}@gcp-sa-pam.iam.gserviceaccount.com"
PAM_AGENT_ROLE = "roles/privilegedaccessmanager.folderServiceAgent"


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
    probe job and read access to that job's results."""
    apis = [
        gcp.projects.Service(
            f"control-staging-{api.split('.')[0]}",
            project=project,
            service=api,
            disable_on_destroy=False,
            opts=opts,
        )
        for api in ("sts.googleapis.com", "iamcredentials.googleapis.com")
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


def build() -> None:
    config = pulumi.Config()
    platform_folder = config.require("platform_folder_id")
    operator = config.get("operator") or n.OPERATOR
    opts = pulumi.ResourceOptions(provider=provider())
    org = f"organizations/{n.ORG_ID}"

    _location_policy(n.PLATFORM_FOLDER, platform_folder, opts)

    cells = _folder(n.CELLS_FOLDER, n.CELLS_FOLDER, org, opts)
    _location_policy(n.CELLS_FOLDER, cells.folder_id, opts)
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
