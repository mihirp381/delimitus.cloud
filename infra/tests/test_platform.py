"""The platform program, run against mocks."""

import json
from typing import Any

import pulumi
import pytest

import mockcloud
from mockcloud import Declared, one, run
from ssc_infra import naming, platform, policies

PLATFORM_FOLDER = "333333333333"
DEPLOYER = naming.sa_email(naming.DEPLOYER, naming.BOOTSTRAP_PROJECT)
DEPLOYER_MEMBER = f"serviceAccount:{DEPLOYER}"
DEPLOYER_IMAGE = (
    f"us-central1-docker.pkg.dev/{naming.BOOTSTRAP_PROJECT}/ssc-platform/deployer@sha256:{'0' * 64}"
)


@pytest.fixture(scope="module")
def declared() -> list[Declared]:
    return run(naming.PLATFORM_STACK, {"platform_folder_id": PLATFORM_FOLDER})


def _folders(declared: list[Declared]) -> dict[str, Declared]:
    return {d.name: d for d in declared if d.type == "gcp:organizations/folder:Folder"}


def test_the_folders_of_decision_021(declared: list[Declared]) -> None:
    folders = _folders(declared)
    assert sorted(folders) == ["ssc-cells", "ssc-cells-prod", "ssc-cells-staging", "ssc-sandbox"]
    assert folders["ssc-cells-prod"].inputs["parent"] == folders["ssc-cells"].outputs["name"]
    assert all(f.inputs["deletionProtection"] is True for f in folders.values())


def test_every_folder_keeps_its_logs_in_region(declared: list[Declared]) -> None:
    settings = {
        d.inputs["folder"]: d.inputs["storageLocation"]
        for d in declared
        if d.type == "gcp:logging/folderSettings:FolderSettings"
    }
    assert settings == {f.outputs["folderId"]: naming.REGION for f in _folders(declared).values()}


def _policies(declared: list[Declared], folder: str) -> dict[str, dict[str, Any]]:
    return {
        d.inputs["name"].rsplit("/", 1)[-1]: d.inputs
        for d in declared
        if d.type == "gcp:orgpolicy/policy:Policy" and d.inputs["parent"] == f"folders/{folder}"
    }


def test_platform_and_cells_allow_only_the_region(declared: list[Declared]) -> None:
    """Cells also allow ``global``: the wildcard certificate and its DNS authorisation exist only
    there, and Certificate Manager checks the location policy (SSC-088)."""
    platform_policies = _policies(declared, PLATFORM_FOLDER)
    assert list(platform_policies) == ["gcp.resourceLocations"]
    assert platform_policies["gcp.resourceLocations"]["spec"]["rules"] == [
        {"values": {"allowedValues": ["in:us-central1-locations"]}}
    ]
    cells = _policies(declared, _folders(declared)["ssc-cells"].outputs["folderId"])
    assert cells["gcp.resourceLocations"]["spec"]["rules"] == [
        {"values": {"allowedValues": ["in:us-central1-locations", "global"]}}
    ]
    locations = one(declared, "gcp:orgpolicy/policy:Policy", "ssc-cells-locations").inputs
    assert locations["name"].endswith("/policies/gcp.resourceLocations")


def test_the_cells_folder_holds_the_policy_table(declared: list[Declared]) -> None:
    cells_id = _folders(declared)["ssc-cells"].outputs["folderId"]
    cells = _policies(declared, cells_id)
    assert set(cells) == {
        "gcp.resourceLocations",
        "storage.publicAccessPrevention",
        "iam.managed.allowedPolicyMembers",
        "iam.disableServiceAccountKeyCreation",
        "iam.automaticIamGrantsForDefaultServiceAccounts",
        "compute.restrictVpcPeering",
        "compute.restrictSharedVpcHostProjects",
        "run.allowedIngress",
        "compute.vmExternalIpAccess",
        "sql.restrictPublicIp",
    }
    for name, policy in cells.items():
        assert policy["name"] == f"folders/{cells_id}/policies/{name}"
    rules = {k: v["spec"]["rules"] for k, v in cells.items()}
    for boolean in (
        "storage.publicAccessPrevention",
        "iam.disableServiceAccountKeyCreation",
        "iam.automaticIamGrantsForDefaultServiceAccounts",
        "sql.restrictPublicIp",
    ):
        assert rules[boolean] == [{"enforce": "TRUE"}]
    for denied in ("compute.restrictSharedVpcHostProjects", "compute.vmExternalIpAccess"):
        assert rules[denied] == [{"denyAll": "TRUE"}]
    assert rules["run.allowedIngress"] == [
        {"values": {"allowedValues": ["is:internal", "is:internal-and-cloud-load-balancing"]}}
    ]
    assert rules["compute.restrictVpcPeering"] == [
        {"values": {"allowedValues": [policies.GOOGLE_PRODUCERS]}}
    ]


def test_the_peering_allowance_is_set_in_config() -> None:
    declared = run(
        naming.PLATFORM_STACK,
        {
            "platform_folder_id": PLATFORM_FOLDER,
            "peering_allowed": json.dumps(["under:organizations/1"]),
        },
    )
    peering = one(declared, "gcp:orgpolicy/policy:Policy", "ssc-cells-vpc-peering").inputs
    assert peering["spec"]["rules"] == [{"values": {"allowedValues": ["under:organizations/1"]}}]


def test_only_the_operator_and_the_org_may_hold_roles_in_a_cell(declared: list[Declared]) -> None:
    members = one(declared, "gcp:orgpolicy/policy:Policy", "ssc-cells-policy-members").inputs
    enforced, exception = members["spec"]["rules"]
    assert enforced["enforce"] == "TRUE"
    assert json.loads(enforced["parameters"]) == {
        "allowedMemberSubjects": [naming.OPERATOR],
        "allowedPrincipalSets": [
            f"//cloudresourcemanager.googleapis.com/organizations/{naming.ORG_ID}"
        ],
    }
    key = one(declared, "gcp:tags/tagKey:TagKey").outputs["name"]
    value = one(declared, "gcp:tags/tagValue:TagValue").outputs["name"]
    assert exception == {
        "enforce": "FALSE",
        "condition": {
            "title": "the cell gateway's public invoker",
            "expression": f"resource.matchTagId('tagKeys/{key}', 'tagValues/{value}')",
        },
    }


def test_only_the_operator_and_the_deployer_may_bind_the_public_invoker_tag(
    declared: list[Declared],
) -> None:
    key = one(declared, "gcp:tags/tagKey:TagKey")
    assert (key.inputs["parent"], key.inputs["shortName"]) == (
        f"organizations/{naming.ORG_ID}",
        "ssc-public-invoker",
    )
    value = one(declared, "gcp:tags/tagValue:TagValue")
    assert (value.inputs["parent"], value.inputs["shortName"]) == (
        f"tagKeys/{key.outputs['name']}",
        "gateway",
    )
    binders = one(declared, "gcp:tags/tagValueIamBinding:TagValueIamBinding").inputs
    assert binders == {
        "tagValue": f"tagValues/{value.outputs['name']}",
        "role": "roles/resourcemanager.tagUser",
        "members": [naming.OPERATOR, DEPLOYER_MEMBER],
    }
    assert not [d for d in declared if d.type == "gcp:tags/tagValueIamMember:TagValueIamMember"]


def test_the_folder_deny_rule_names_the_control_plane(declared: list[Declared]) -> None:
    """Every control-plane account: the API, the worker, the auth host and the migration job."""
    deny = one(declared, "gcp:iam/denyPolicy:DenyPolicy").inputs
    cells = _folders(declared)["ssc-cells"].outputs["folderId"]
    assert deny["parent"] == f"cloudresourcemanager.googleapis.com%2Ffolders%2F{cells}"
    rule = deny["rules"][0]["denyRule"]
    assert rule["deniedPermissions"] == ["secretmanager.googleapis.com/versions.access"]
    assert rule["deniedPrincipals"] == [
        "principal://iam.googleapis.com/projects/-/serviceAccounts/"
        f"{account}@ssc-control-staging.iam.gserviceaccount.com"
        for account in ("ssc-control", "ssc-control-worker", "ssc-auth", "ssc-control-migrate")
    ]


def test_staff_access_is_just_in_time(declared: list[Declared]) -> None:
    jit = one(declared, "gcp:privilegedaccessmanager/entitlement:entitlement").inputs
    assert jit["maxRequestDuration"] == "3600s"
    assert "approvalWorkflow" not in jit
    assert jit["eligibleUsers"] == [{"principals": [naming.OPERATOR]}]
    assert jit["requesterJustificationConfig"] == {"unstructured": {}}
    assert (
        jit["privilegedAccess"]["gcpIamAccess"]["resourceType"]
        == "cloudresourcemanager.googleapis.com/Folder"
    )
    assert jit["privilegedAccess"]["gcpIamAccess"]["roleBindings"] == [{"role": "roles/writer"}]


def test_the_budget_is_250_a_month_over_every_ssc_folder(declared: list[Declared]) -> None:
    budget = one(declared, "gcp:billing/budget:Budget").inputs
    assert budget["amount"] == {"specifiedAmount": {"units": "250", "currencyCode": "USD"}}
    folders = _folders(declared)
    assert sorted(budget["budgetFilter"]["resourceAncestors"]) == sorted(
        [
            f"folders/{PLATFORM_FOLDER}",
            f"folders/{folders['ssc-cells'].outputs['folderId']}",
            f"folders/{folders['ssc-sandbox'].outputs['folderId']}",
        ]
    )
    assert {"thresholdPercent": 1.0, "spendBasis": "FORECASTED_SPEND"} in budget["thresholdRules"]


def test_the_control_plane_signs_bundle_urls_as_itself(declared: list[Declared]) -> None:
    grant = one(declared, "gcp:serviceaccount/iAMMember:IAMMember", "control-staging-signs-urls")
    control = "ssc-control@ssc-control-staging.iam.gserviceaccount.com"
    assert grant.inputs["role"] == "roles/iam.serviceAccountTokenCreator"
    assert grant.inputs["member"] == f"serviceAccount:{control}"
    assert grant.inputs["serviceAccountId"].endswith(f"/serviceAccounts/{control}")


def test_the_control_project_is_protected(declared: list[Declared]) -> None:
    project = one(declared, "gcp:organizations/project:Project").inputs
    assert project["projectId"] == "ssc-control-staging"
    assert project["folderId"] == PLATFORM_FOLDER
    assert project["deletionPolicy"] == "PREVENT"
    assert project["billingAccount"] == naming.BILLING_ACCOUNT


def test_staging_may_run_without_billing_while_it_has_no_control_plane() -> None:
    """Staging holds only accounts, IAM and free APIs while no control plane runs there, so it
    can give its billing slot to prod (SSC-089: one account holds five projects)."""
    unbilled = run(
        naming.PLATFORM_STACK,
        {"platform_folder_id": PLATFORM_FOLDER, "control_staging_billing": "false"},
    )
    project = one(unbilled, "gcp:organizations/project:Project").inputs
    assert project["projectId"] == "ssc-control-staging"
    assert "billingAccount" not in project or project["billingAccount"] is None
    staged = run(
        naming.PLATFORM_STACK,
        {
            "platform_folder_id": PLATFORM_FOLDER,
            "control_staging_billing": "false",
            "control_stages": '["staging"]',
        },
    )
    project = one(staged, "gcp:organizations/project:Project").inputs
    assert project["billingAccount"] == naming.BILLING_ACCOUNT


def test_the_pam_service_agent_manages_the_cells_folder(declared: list[Declared]) -> None:
    grant = one(declared, "gcp:folder/iAMMember:IAMMember", "cells-pam-agent").inputs
    assert grant["role"] == "roles/privilegedaccessmanager.folderServiceAgent"
    assert (
        grant["member"]
        == f"serviceAccount:service-org-{naming.ORG_ID}@gcp-sa-pam.iam.gserviceaccount.com"
    )
    assert grant["folder"] == _folders(declared)["ssc-cells"].outputs["name"]


def test_the_nightly_account_reads_the_cells_folders_policies_and_roles(
    declared: list[Declared],
) -> None:
    nightly = one(declared, "gcp:serviceaccount/account:Account", "nightly-sa").outputs["member"]
    folder = _folders(declared)["ssc-cells"].outputs["name"]
    grants = [
        d.inputs
        for d in declared
        if d.type == "gcp:folder/iAMMember:IAMMember" and d.inputs["member"] == nightly
    ]
    assert {g["role"] for g in grants} == {
        "roles/iam.securityReviewer",
        "roles/iam.denyReviewer",
        "roles/orgpolicy.policyViewer",
    }
    assert len(grants) == 3
    assert {g["folder"] for g in grants} == {folder}


def test_only_the_nightly_and_drill_workflows_on_main_become_the_nightly_account(
    declared: list[Declared],
) -> None:
    provider = one(declared, "gcp:iam/workloadIdentityPoolProvider:WorkloadIdentityPoolProvider")
    condition = provider.inputs["attributeCondition"]
    assert f'assertion.repository == "{naming.GITHUB_REPOSITORY}"' in condition
    repo = naming.GITHUB_REPOSITORY
    assert (
        f'assertion.workflow_ref in ["{repo}/.github/workflows/nightly.yml@refs/heads/main", '
        f'"{repo}/.github/workflows/kill-drill.yml@refs/heads/main"]' in condition
    )
    assert provider.inputs["oidc"]["issuerUri"] == "https://token.actions.githubusercontent.com"
    members = {
        d.name: d.inputs for d in declared if d.type == "gcp:serviceaccount/iAMMember:IAMMember"
    }
    assert members["nightly-wif"]["role"] == "roles/iam.workloadIdentityUser"
    assert members["nightly-wif"]["member"].endswith(
        f"/workloadIdentityPools/ssc-github/attribute.repository/{naming.GITHUB_REPOSITORY}"
    )
    tokens = members["nightly-control-id-tokens"]
    assert tokens["role"] == "roles/iam.serviceAccountOpenIdTokenCreator"
    assert tokens["member"] == "serviceAccount:" + naming.sa_email(
        naming.NIGHTLY_SA, naming.control_project("staging")
    )


def test_the_nightly_project_can_read_cell_logs(declared: list[Declared]) -> None:
    logging_api = one(declared, "gcp:projects/service:Service", "control-staging-logging")
    assert logging_api.inputs["service"] == "logging.googleapis.com"
    assert logging_api.inputs["project"] == naming.control_project("staging")


def test_the_two_public_zones_live_in_the_platform_project(declared: list[Declared]) -> None:
    zones = {
        d.inputs["name"]: d.inputs for d in declared if d.type == "gcp:dns/managedZone:ManagedZone"
    }
    assert {k: v["dnsName"] for k, v in zones.items()} == {
        "delimitusapps": "delimitusapps.com.",
        "delimitus": "delimitus.com.",
    }
    for zone in zones.values():
        assert zone["project"] == naming.BOOTSTRAP_PROJECT
        assert zone["visibility"] == "public"
        assert zone["dnssecConfig"] == {"state": "off"}


def test_cell_stacks_may_write_records_only_in_the_apps_zone(declared: list[Declared]) -> None:
    grants = [
        d.inputs
        for d in declared
        if d.type == "gcp:dns/dnsManagedZoneIamMember:DnsManagedZoneIamMember"
    ]
    assert {(g["project"], g["managedZone"], g["member"]) for g in grants} == {
        (naming.BOOTSTRAP_PROJECT, naming.APPS_ZONE, naming.OPERATOR),
        (naming.BOOTSTRAP_PROJECT, naming.APPS_ZONE, DEPLOYER_MEMBER),
    }
    assert grants[0]["role"] == grants[1]["role"]
    grant = grants[0]
    role = one(declared, "gcp:projects/iAMCustomRole:IAMCustomRole", "apps-zone-records").inputs
    assert grant["role"] == f"projects/{naming.BOOTSTRAP_PROJECT}/roles/{role['roleId']}"
    assert all(p.startswith("dns.") for p in role["permissions"])
    zone_powers = {p for p in role["permissions"] if p.startswith("dns.managedZones.")}
    assert zone_powers == {"dns.managedZones.get"}
    project_grants = [d for d in declared if d.type == "gcp:projects/iAMMember:IAMMember"]
    assert not [g for g in project_grants if g.inputs["role"] == grant["role"]]


def test_the_zone_name_servers_are_exported(monkeypatch: pytest.MonkeyPatch) -> None:
    exported: dict[str, object] = {}
    monkeypatch.setattr(pulumi, "export", lambda name, value: exported.__setitem__(name, value))
    run(naming.PLATFORM_STACK, {"platform_folder_id": PLATFORM_FOLDER})
    assert {"apps_zone_name_servers", "platform_zone_name_servers"} <= set(exported)


def test_the_tag_and_the_policy_table_are_exported(monkeypatch: pytest.MonkeyPatch) -> None:
    exported: dict[str, Any] = {}
    monkeypatch.setattr(pulumi, "export", lambda name, value: exported.__setitem__(name, value))
    run(naming.PLATFORM_STACK, {"platform_folder_id": PLATFORM_FOLDER})
    assert {"public_invoker_tag", "cell_policies"} <= set(exported)
    assert exported["cell_policies"] == policies.summaries(policies.cell_rules(naming.OPERATOR))
    assert exported["cell_policies"]["compute.vmExternalIpAccess"] == "deny all"


def _grants_to(declared: list[Declared], member: str) -> set[tuple[str, str]]:
    return {
        (d.type.split(":")[1], d.inputs["role"])
        for d in declared
        if d.inputs.get("member") == member or member in (d.inputs.get("members") or [])
    }


def test_the_deployer_holds_exactly_what_applying_a_lazy_flag_needs(
    declared: list[Declared],
) -> None:
    """State and its key, quota here, a fixed list on the cells folder, the apps zone's records
    and the public-invoker tag. No billing, deny-rule or secret role; nothing on the org."""
    account = one(declared, "gcp:serviceaccount/account:Account", "deployer-sa").inputs
    assert (account["project"], account["accountId"]) == (
        naming.BOOTSTRAP_PROJECT,
        "ssc-cell-deployer",
    )
    zone_role = one(declared, "gcp:projects/iAMCustomRole:IAMCustomRole", "apps-zone-records")
    assert _grants_to(declared, DEPLOYER_MEMBER) == {
        ("storage/bucketIAMMember", "roles/storage.objectAdmin"),
        ("kms/cryptoKeyIAMMember", "roles/cloudkms.cryptoKeyEncrypterDecrypter"),
        ("projects/iAMMember", "roles/serviceusage.serviceUsageConsumer"),
        ("dns/dnsManagedZoneIamMember", zone_role.outputs["name"]),
        ("tags/tagValueIamBinding", "roles/resourcemanager.tagUser"),
        *(("folder/iAMMember", r) for r in platform.DEPLOYER_FOLDER_ROLES),
    }
    cells = _folders(declared)["ssc-cells"].outputs["name"]
    folder_grants = [
        d.inputs
        for d in declared
        if d.type == "gcp:folder/iAMMember:IAMMember" and d.inputs["member"] == DEPLOYER_MEMBER
    ]
    assert {g["folder"] for g in folder_grants} == {cells}
    state = one(declared, "gcp:storage/bucketIAMMember:BucketIAMMember", "deployer-state").inputs
    assert state["bucket"] == naming.STATE_BUCKET
    key = one(declared, "gcp:kms/cryptoKeyIAMMember:CryptoKeyIAMMember", "deployer-state-key")
    assert f"gcpkms://{key.inputs['cryptoKeyId']}" == naming.SECRETS_PROVIDER
    quota = one(declared, "gcp:projects/iAMMember:IAMMember", "deployer-quota").inputs
    assert quota["project"] == naming.BOOTSTRAP_PROJECT
    forbidden = ("billing", "owner", "editor", "denyAdmin", "secretmanager", "organization")
    assert not [
        r for _, r in _grants_to(declared, DEPLOYER_MEMBER) if any(f in r for f in forbidden)
    ]


def test_the_operator_keeps_its_grants(declared: list[Declared]) -> None:
    assert (
        "dns/dnsManagedZoneIamMember",
        one(declared, "gcp:projects/iAMCustomRole:IAMCustomRole", "apps-zone-records").outputs[
            "name"
        ],
    ) in _grants_to(declared, naming.OPERATOR)
    assert ("tags/tagValueIamBinding", platform.TAG_USER) in _grants_to(declared, naming.OPERATOR)


def test_no_deployer_job_until_its_image_is_named(declared: list[Declared]) -> None:
    assert not [d for d in declared if d.type == "gcp:cloudrunv2/job:Job"]
    registry = one(declared, "gcp:artifactregistry/repository:Repository", "platform-registry")
    assert (registry.inputs["project"], registry.inputs["repositoryId"]) == (
        naming.BOOTSTRAP_PROJECT,
        naming.PLATFORM_REPOSITORY,
    )


def test_the_worker_may_only_start_the_deployer_job_and_read_it() -> None:
    """The worker runs the cell jobs (SSC-087), so it alone starts the job (SSC-064)."""
    declared = run(
        naming.PLATFORM_STACK,
        {"platform_folder_id": PLATFORM_FOLDER, "deployer_image": DEPLOYER_IMAGE},
    )
    job = one(declared, "gcp:cloudrunv2/job:Job", "cell-deployer").inputs
    assert (job["project"], job["name"]) == (naming.BOOTSTRAP_PROJECT, naming.DEPLOYER)
    template = job["template"]["template"]
    assert template["serviceAccount"] == DEPLOYER
    assert template["maxRetries"] == 0
    (container,) = template["containers"]
    assert container["image"] == DEPLOYER_IMAGE
    assert "commands" not in container
    assert "args" not in container
    worker = f"serviceAccount:{mockcloud.WORKERS['staging']}"
    assert _grants_to(declared, worker) == {
        ("cloudrunv2/jobIamMember", "roles/run.jobsExecutorWithOverrides"),
        ("cloudrunv2/jobIamMember", "roles/run.viewer"),
        ("serviceaccount/iAMMember", "roles/iam.serviceAccountTokenCreator"),
    }
    control = f"serviceAccount:{mockcloud.CONTROL['staging']}"
    assert _grants_to(declared, control) == {
        ("serviceaccount/iAMMember", "roles/iam.serviceAccountTokenCreator"),
    }


def test_the_deployer_is_exported(monkeypatch: pytest.MonkeyPatch) -> None:
    exported: dict[str, Any] = {}
    monkeypatch.setattr(pulumi, "export", lambda name, value: exported.__setitem__(name, value))
    run(naming.PLATFORM_STACK, {"platform_folder_id": PLATFORM_FOLDER})
    assert exported["cell_deployer"]["job"] == (
        f"projects/{naming.BOOTSTRAP_PROJECT}/locations/{naming.REGION}/jobs/ssc-cell-deployer"
    )
    assert exported["cell_deployer"]["job"] == naming.deployer_job()
