"""The platform program, run against mocks."""

import json
from typing import Any

import pulumi
import pytest

from mockcloud import Declared, one, run
from ssc_infra import naming, policies

PLATFORM_FOLDER = "333333333333"


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


def test_only_the_operator_may_bind_the_public_invoker_tag(declared: list[Declared]) -> None:
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
        "members": [naming.OPERATOR],
    }
    assert not [d for d in declared if d.type == "gcp:tags/tagValueIamMember:TagValueIamMember"]


def test_the_folder_deny_rule_names_the_control_plane(declared: list[Declared]) -> None:
    deny = one(declared, "gcp:iam/denyPolicy:DenyPolicy").inputs
    cells = _folders(declared)["ssc-cells"].outputs["folderId"]
    assert deny["parent"] == f"cloudresourcemanager.googleapis.com%2Ffolders%2F{cells}"
    rule = deny["rules"][0]["denyRule"]
    assert rule["deniedPermissions"] == ["secretmanager.googleapis.com/versions.access"]
    assert rule["deniedPrincipals"] == [
        "principal://iam.googleapis.com/projects/-/serviceAccounts/"
        "ssc-control@ssc-control-staging.iam.gserviceaccount.com"
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


def test_the_pam_service_agent_manages_the_cells_folder(declared: list[Declared]) -> None:
    grant = one(declared, "gcp:folder/iAMMember:IAMMember").inputs
    assert grant["role"] == "roles/privilegedaccessmanager.folderServiceAgent"
    assert (
        grant["member"]
        == f"serviceAccount:service-org-{naming.ORG_ID}@gcp-sa-pam.iam.gserviceaccount.com"
    )
    assert grant["folder"] == _folders(declared)["ssc-cells"].outputs["name"]


def test_only_the_nightly_workflow_on_main_becomes_the_nightly_account(
    declared: list[Declared],
) -> None:
    provider = one(declared, "gcp:iam/workloadIdentityPoolProvider:WorkloadIdentityPoolProvider")
    condition = provider.inputs["attributeCondition"]
    assert f'assertion.repository == "{naming.GITHUB_REPOSITORY}"' in condition
    assert (
        f'assertion.workflow_ref == "{naming.GITHUB_REPOSITORY}/.github/workflows/nightly.yml'
        '@refs/heads/main"' in condition
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
    grant = one(declared, "gcp:dns/dnsManagedZoneIamMember:DnsManagedZoneIamMember").inputs
    assert (grant["project"], grant["managedZone"], grant["member"]) == (
        naming.BOOTSTRAP_PROJECT,
        naming.APPS_ZONE,
        naming.OPERATOR,
    )
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
