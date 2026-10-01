"""The cell program, run against mocks: same shape for every label, and the rules SSC-013 names."""

from typing import Any, cast

import pulumi
import pytest

from mockcloud import Declared, as_export, one, run
from ssc_infra import cell, cell_diff, naming

A, B = "testcell01", "testcell02"
ALL = {"probe": "true"}


@pytest.fixture(scope="module")
def cell_a() -> list[Declared]:
    return run(naming.cell_stack(A), ALL)


@pytest.fixture(scope="module")
def cell_b() -> list[Declared]:
    return run(naming.cell_stack(B), ALL)


def test_two_cells_differ_only_in_their_label(
    cell_a: list[Declared], cell_b: list[Declared]
) -> None:
    first = cell_diff.normalise(as_export(cell_a, A), A)
    second = cell_diff.normalise(as_export(cell_b, B), B)
    assert len(first) > 70
    assert cell_diff.compare(first, second) == []


def test_the_diff_notices_a_drifted_cell(cell_a: list[Declared], cell_b: list[Declared]) -> None:
    drifted = [
        Declared(d.type, d.name, {**d.inputs, "tier": "db-custom-2-7680"}, d.outputs)
        if d.type == "gcp:sql/databaseInstance:DatabaseInstance"
        else d
        for d in cell_b
    ]
    first = cell_diff.normalise(as_export(cell_a, A), A)
    second = cell_diff.normalise(as_export(drifted[:-1], B), B)
    diffs = cell_diff.compare(first, second)
    assert any("only in first" in d for d in diffs)
    assert any("in.tier" in d for d in diffs)


def test_everything_is_named_from_the_label(cell_a: list[Declared]) -> None:
    project = one(cell_a, "gcp:organizations/project:Project").inputs
    assert project["projectId"] == "ssc-c-testcell01"
    assert project["labels"] == {"ssc-cell": A, "ssc-stage": "staging"}
    assert project["folderId"] == "222222222222"
    assert project["autoCreateNetwork"] is False
    assert one(cell_a, "gcp:storage/bucket:Bucket").inputs["name"] == "ssc-c-testcell01-cell"


def test_every_resource_stays_in_the_region(cell_a: list[Declared]) -> None:
    for d in cell_a:
        for key in ("region", "location"):
            if key in d.inputs:
                assert d.inputs[key].lower() == naming.REGION, (d.type, d.name)


def test_the_database_is_private_encrypted_and_iam_only(cell_a: list[Declared]) -> None:
    sql = one(cell_a, "gcp:sql/databaseInstance:DatabaseInstance").inputs
    assert sql["databaseVersion"] == "POSTGRES_18"
    assert sql["encryptionKeyName"] == "key-sql-id"
    ip = sql["settings"]["ipConfiguration"]
    assert ip["ipv4Enabled"] is False
    assert ip["sslMode"] == "ENCRYPTED_ONLY"
    assert {"name": "cloudsql.iam_authentication", "value": "on"} in sql["settings"][
        "databaseFlags"
    ]
    assert "rootPassword" not in sql
    user = one(cell_a, "gcp:sql/user:User").inputs
    assert user["type"] == "CLOUD_IAM_SERVICE_ACCOUNT"
    assert "password" not in user


def test_the_registry_uses_the_cell_key(cell_a: list[Declared]) -> None:
    assert (
        one(cell_a, "gcp:artifactregistry/repository:Repository").inputs["kmsKeyName"]
        == "key-registry-id"
    )


def test_the_network_is_ipv4_with_a_fixed_ip_per_nat(cell_a: list[Declared]) -> None:
    subnets = {
        d.inputs["name"]: d.inputs for d in cell_a if d.type == "gcp:compute/subnetwork:Subnetwork"
    }
    assert subnets["apps"]["ipCidrRange"] == "10.20.0.0/22"
    assert subnets["apps"]["stackType"] == "IPV4_ONLY"
    nats = [d.inputs for d in cell_a if d.type == "gcp:compute/routerNat:RouterNat"]
    assert sorted(n["name"] for n in nats) == ["nat-apps", "nat-gateway"]
    assert all(n["natIpAllocateOption"] == "MANUAL_ONLY" and len(n["natIps"]) == 1 for n in nats)


def test_destroy_leaves_the_sql_peering_to_the_project(cell_a: list[Declared]) -> None:
    assert (
        one(cell_a, "gcp:servicenetworking/connection:Connection").inputs["deletionPolicy"]
        == "ABANDON"
    )


def test_egress_is_denied_unless_allowed(cell_a: list[Declared]) -> None:
    rules = {
        d.inputs["name"]: d.inputs for d in cell_a if d.type == "gcp:compute/firewall:Firewall"
    }
    assert rules["egress-deny-all"]["denies"] == [{"protocol": "all"}]
    assert rules["egress-deny-all"]["destinationRanges"] == ["0.0.0.0/0"]
    assert rules["egress-gateway"]["targetTags"] == ["ssc-gateway"]


def test_the_gateway_is_internal_always_on_and_behind_the_load_balancer(
    cell_a: list[Declared],
) -> None:
    gw = one(cell_a, "gcp:cloudrunv2/service:Service", "ssc-gateway").inputs
    assert gw["ingress"] == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    assert gw["template"]["scaling"]["minInstanceCount"] == 2
    assert gw["scaling"]["maxInstanceCount"] == 20
    assert gw["template"]["containers"][0]["resources"]["cpuIdle"] is False
    assert gw["template"]["vpcAccess"]["egress"] == "ALL_TRAFFIC"
    rule = one(cell_a, "gcp:compute/forwardingRule:ForwardingRule").inputs
    assert rule["loadBalancingScheme"] == "INTERNAL_MANAGED"


def test_the_cell_agent_scales_to_zero_with_a_pinned_ceiling(cell_a: list[Declared]) -> None:
    agent = one(cell_a, "gcp:cloudrunv2/service:Service", "ssc-cell-agent").inputs
    assert agent["template"]["scaling"]["minInstanceCount"] == 0
    assert agent["scaling"]["maxInstanceCount"] == cell.AGENT_MAX


def test_the_diff_ignores_assigned_ids_and_nulls(
    cell_a: list[Declared], cell_b: list[Declared]
) -> None:
    assigned = {
        "numericId": "1",
        "generatedId": 2,
        "creationTime": "2026-10-01T00:00:00Z",
        "annotations": None,
    }
    renumbered = [Declared(d.type, d.name, d.inputs, {**d.outputs, **assigned}) for d in cell_b]
    first = cell_diff.normalise(as_export(cell_a, A), A)
    second = cell_diff.normalise(as_export(renumbered, B), B)
    assert cell_diff.compare(first, second) == []


def test_only_the_control_plane_invokes_the_cell_agent(cell_a: list[Declared]) -> None:
    invoker = one(cell_a, "gcp:cloudrunv2/serviceIamMember:ServiceIamMember").inputs
    assert (
        invoker["member"]
        == "serviceAccount:ssc-control@ssc-control-staging.iam.gserviceaccount.com"
    )


def _grants(declared: list[Declared], member: str) -> dict[str, dict[str, str] | None]:
    return {
        d.inputs["role"]: d.inputs.get("condition")
        for d in declared
        if d.type == "gcp:projects/iAMMember:IAMMember" and d.inputs["member"] == member
    }


def test_the_control_plane_only_adds_secret_versions(cell_a: list[Declared]) -> None:
    grants = _grants(
        cell_a, "serviceAccount:ssc-control@ssc-control-staging.iam.gserviceaccount.com"
    )
    assert list(grants) == ["roles/secretmanager.secretVersionAdder"]
    condition = grants["roles/secretmanager.secretVersionAdder"]
    assert condition is not None
    assert '.startsWith("ssc-a-")' in condition["expression"]


def test_the_cell_agent_manages_only_ssc_a_resources(cell_a: list[Declared]) -> None:
    agent = f"serviceAccount:{naming.sa_email('ssc-cell-agent', naming.cell_project(A))}"
    grants = _grants(cell_a, agent)
    for role in (
        "roles/secretmanager.admin",
        "roles/run.admin",
        "roles/iam.serviceAccountAdmin",
        "roles/iam.serviceAccountUser",
    ):
        condition = grants[role]
        assert condition is not None, role
        assert '.startsWith("ssc-a-")' in condition["expression"], role
    create = one(cell_a, "gcp:projects/iAMCustomRole:IAMCustomRole").inputs
    assert all(p.endswith(".create") for p in create["permissions"])


def test_the_cell_deny_rule_names_every_ssc_identity(cell_a: list[Declared]) -> None:
    rule = one(cell_a, "gcp:iam/denyPolicy:DenyPolicy").inputs["rules"][0]["denyRule"]
    assert rule["deniedPermissions"] == [naming.SECRET_READ]
    denied = {p.rsplit("/", 1)[-1].split("@")[0] for p in rule["deniedPrincipals"]}
    assert denied == {"ssc-gateway", "ssc-cell-agent", "ssc-build", naming.PROBE_DENIED_SA}


def test_the_probe_value_never_reaches_the_state(cell_a: list[Declared]) -> None:
    version = one(cell_a, "gcp:secretmanager/secretVersion:SecretVersion").inputs
    assert "secretData" not in version
    assert "secretDataWo" in version
    readers = {
        d.inputs["member"]
        for d in cell_a
        if d.type == "gcp:secretmanager/secretIamMember:SecretIamMember"
    }
    assert len(readers) == 2


def test_a_cell_without_the_probe_has_no_probe_secret() -> None:
    declared = run(naming.cell_stack("testcell03"))
    assert not [d for d in declared if d.type.startswith("gcp:secretmanager/")]


@pytest.mark.parametrize(
    ("stage", "policy", "protected"), [("staging", "DELETE", False), ("prod", "PREVENT", True)]
)
def test_only_staging_cells_can_be_destroyed(stage: str, policy: str, protected: bool) -> None:
    declared = run(naming.cell_stack("testcell04"), {"stage": stage})
    assert one(declared, "gcp:organizations/project:Project").inputs["deletionPolicy"] == policy
    assert (
        one(declared, "gcp:sql/databaseInstance:DatabaseInstance").inputs["deletionProtection"]
        is protected
    )


def test_a_prod_cell_needs_a_prod_control_plane() -> None:
    assert cell.control_for({"prod": "x"}, "prod") == "x"
    with pytest.raises(ValueError, match="no prod control plane"):
        cell.control_for({"staging": "x"}, "prod")


@pytest.mark.parametrize("stack", ["c-t01", "cell-testcell01", "c-TESTCELL01"])
def test_a_stack_must_name_a_valid_cell_label(stack: str) -> None:
    with pytest.raises(ValueError):
        naming.label_of_stack(stack)


def _options(monkeypatch: pytest.MonkeyPatch) -> dict[str, pulumi.ResourceOptions]:
    """Each resource's options, which the mocks do not see."""
    seen: dict[str, pulumi.ResourceOptions] = {}
    create = pulumi.CustomResource.__init__

    def spy(
        self: pulumi.CustomResource,
        t: str,
        name: str,
        props: Any = None,
        opts: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        seen[f"{t}::{name}"] = opts or pulumi.ResourceOptions()
        create(self, t, name, props, opts, *args, **kwargs)

    monkeypatch.setattr(pulumi.CustomResource, "__init__", spy)
    run(naming.cell_stack(A), ALL)
    return seen


def test_nothing_is_created_before_the_cell_apis_are_on(monkeypatch: pytest.MonkeyPatch) -> None:
    apis = {api.split(".")[0] for api in cell.APIS}
    first = {"gcp:organizations/project:Project::project", "pulumi:providers:gcp::gcp"}
    first |= {f"gcp:projects/service:Service::{api}" for api in apis}
    late = {
        k: apis - {d._name for d in cast(list[pulumi.Resource], o.depends_on or [])}  # pyright: ignore[reportPrivateUsage]
        for k, o in _options(monkeypatch).items()
        if k not in first and not k.startswith("pulumi:pulumi")
    }
    assert late
    assert {k: v for k, v in late.items() if v} == {}


def test_destroy_leaves_the_network_to_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    kept = {k for k, o in _options(monkeypatch).items() if o.retain_on_delete}
    assert kept == {
        "gcp:compute/network:Network::vpc",
        "gcp:compute/subnetwork:Subnetwork::subnet-apps",
        "gcp:compute/subnetwork:Subnetwork::subnet-gateway",
    }
