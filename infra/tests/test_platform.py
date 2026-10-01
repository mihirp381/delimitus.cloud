"""The platform program, run against mocks."""

import pytest

from mockcloud import Declared, one, run
from ssc_infra import naming

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


def test_platform_and_cells_allow_only_the_region(declared: list[Declared]) -> None:
    policies = {
        d.inputs["parent"]: d.inputs for d in declared if d.type == "gcp:orgpolicy/policy:Policy"
    }
    cells = _folders(declared)["ssc-cells"].outputs["folderId"]
    assert set(policies) == {f"folders/{PLATFORM_FOLDER}", f"folders/{cells}"}
    for policy in policies.values():
        assert policy["name"].endswith("/policies/gcp.resourceLocations")
        assert policy["spec"]["rules"] == [
            {"values": {"allowedValues": ["in:us-central1-locations"]}}
        ]


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
    (grant,) = [d for d in declared if d.type == "gcp:serviceaccount/iAMMember:IAMMember"]
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
