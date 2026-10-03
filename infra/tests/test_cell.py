"""The cell program, run against mocks: same shape for every label, and the rules SSC-013 names."""

import json
from typing import Any, cast

import pulumi
import pytest

import mockcloud
from mockcloud import Declared, as_export, entry_address, one, project_number, run
from ssc_infra import cell, cell_diff, naming
from ssc_shared.hosts import check_apps_domain, parse_app_host, slug_problem

A, B = "testcell01", "testcell02"
LAZY = {"database": "true", "egress": "true", "connections": "true"}
ALL = {"probe": "true"} | LAZY
EMPTY = {"probe": "true"}
FULL_FLAGS = {
    "database": True,
    "egress": True,
    "connections": True,
    "gateway_min": 0,
    "warm": False,
}
EMPTY_FLAGS = {
    "database": False,
    "egress": False,
    "connections": False,
    "gateway_min": 0,
    "warm": False,
}
NEG = "gcp:compute/regionNetworkEndpointGroup:RegionNetworkEndpointGroup"
BACKEND = "gcp:compute/backendService:BackendService"
RECORD = "gcp:dns/recordSet:RecordSet"
TAG_BINDING = "gcp:tags/locationTagBinding:LocationTagBinding"
LB_KINDS = {
    "gcp:compute/backendService:BackendService",
    "gcp:compute/globalForwardingRule:GlobalForwardingRule",
}
ENTRY_RESOURCES = {
    "gcp:compute/globalAddress:GlobalAddress::entry-ip",
    f"{NEG}::gateway-neg",
    f"{NEG}::agent-neg",
    f"{BACKEND}::gateway-backend",
    f"{BACKEND}::agent-backend",
    "gcp:compute/sSLPolicy:SSLPolicy::entry-tls",
    "gcp:compute/uRLMap:URLMap::entry-map",
    "gcp:compute/uRLMap:URLMap::entry-redirect",
    "gcp:compute/targetHttpsProxy:TargetHttpsProxy::entry-https",
    "gcp:compute/targetHttpProxy:TargetHttpProxy::entry-http",
    "gcp:compute/globalForwardingRule:GlobalForwardingRule::entry-https",
    "gcp:compute/globalForwardingRule:GlobalForwardingRule::entry-http",
    "gcp:certificatemanager/dnsAuthorization:DnsAuthorization::cert-dns-auth",
    "gcp:certificatemanager/certificate:Certificate::cert",
    "gcp:certificatemanager/certificateMap:CertificateMap::cert-map",
    "gcp:certificatemanager/certificateMapEntry:CertificateMapEntry::cert-map-entry",
    f"{RECORD}::dns-cert-auth",
    f"{RECORD}::dns-wildcard",
    "gcp:cloudrunv2/serviceIamMember:ServiceIamMember::gateway-invoker",
    f"{TAG_BINDING}::gateway-public-tag",
}
AGENT_ENV = {  # what ssc_agent.__main__ reads
    "SSC_CELL_PROJECT",
    "SSC_CELL_REGION",
    "SSC_CELL_NETWORK",
    "SSC_CELL_SUBNETWORK",
    "SSC_IMAGE_REPOSITORY",
    "SSC_GATEWAY_SA",
}
TOOLS_IMAGE = f"{naming.platform_registry()}/ssc-build-tools@sha256:" + "d" * 64
FRONTEND_IMAGE = f"{naming.platform_registry()}/railpack-frontend@sha256:" + "e" * 64
BUILD = {"build_tools_image": TOOLS_IMAGE, "build_frontend_image": FRONTEND_IMAGE}
BUILD_ENV = {"SSC_BUILD_SA", "SSC_BUILD_TOOLS_IMAGE", "SSC_BUILD_FRONTEND_IMAGE"}


@pytest.fixture(scope="module")
def cell_a() -> list[Declared]:
    return run(naming.cell_stack(A), ALL)


@pytest.fixture(scope="module")
def cell_b() -> list[Declared]:
    return run(naming.cell_stack(B), ALL)


@pytest.fixture(scope="module")
def empty_b() -> list[Declared]:
    return run(naming.cell_stack(B), EMPTY)


def _names(declared: list[Declared]) -> set[str]:
    return {f"{d.type}::{d.name}" for d in declared}


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


def test_the_registries_use_the_cell_key(cell_a: list[Declared]) -> None:
    repos = [d.inputs for d in cell_a if d.type == "gcp:artifactregistry/repository:Repository"]
    assert sorted(r["repositoryId"] for r in repos) == ["ssc-apps", "ssc-platform"]
    assert all(r["kmsKeyName"] == "key-registry-id" for r in repos)


def test_the_network_is_two_ipv4_slash_24s_with_no_proxy_only_subnet(
    cell_a: list[Declared],
) -> None:
    subnets = {
        d.inputs["name"]: d.inputs for d in cell_a if d.type == "gcp:compute/subnetwork:Subnetwork"
    }
    assert {k: v["ipCidrRange"] for k, v in subnets.items()} == {
        "apps": "10.20.0.0/24",
        "gateway": "10.20.4.0/24",
    }
    assert {v["stackType"] for v in subnets.values()} == {"IPV4_ONLY"}
    assert not any("purpose" in v for v in subnets.values())


@pytest.mark.parametrize("config", [EMPTY, ALL])
def test_nat_and_the_fixed_ip_always_exist_for_the_edge_subnet_only(
    config: dict[str, str],
) -> None:
    declared = run(naming.cell_stack(A), config)
    nat = one(declared, "gcp:compute/routerNat:RouterNat").inputs
    assert nat["natIpAllocateOption"] == "MANUAL_ONLY"
    assert len(nat["natIps"]) == 1
    assert [s["name"] for s in nat["subnetworks"]] == ["subnet-gateway-id"]
    fixed = one(declared, "gcp:compute/address:Address", "nat-ip-gateway").inputs
    assert (fixed["addressType"], fixed["networkTier"]) == ("EXTERNAL", "PREMIUM")


def test_the_proxy_data_gateway_and_database_addresses_are_reserved_at_onboarding(
    empty_b: list[Declared],
) -> None:
    reserved = {
        d.inputs["name"]: (d.inputs["address"], d.inputs["subnetwork"])
        for d in empty_b
        if d.type == "gcp:compute/address:Address" and d.inputs["addressType"] == "INTERNAL"
    }
    assert reserved == {
        "ssc-proxy": ("10.20.4.10", "subnet-gateway-id"),
        "ssc-datagw": ("10.20.4.11", "subnet-gateway-id"),
    }
    psa = one(empty_b, "gcp:compute/globalAddress:GlobalAddress", "psa-range").inputs
    assert (psa["address"], psa["prefixLength"], psa["purpose"]) == ("10.21.0.0", 20, "VPC_PEERING")
    one(empty_b, "gcp:servicenetworking/connection:Connection")


def test_the_firewall_is_the_same_before_and_after_every_lazy_resource(
    cell_a: list[Declared], empty_b: list[Declared]
) -> None:
    def rules(declared: list[Declared]) -> dict[str, dict[str, Any]]:
        return {
            d.inputs["name"]: {k: v for k, v in d.inputs.items() if k != "project"}
            for d in declared
            if d.type == "gcp:compute/firewall:Firewall"
        }

    assert rules(cell_a) == rules(empty_b)
    full = rules(cell_a)
    assert full["ingress-proxy"]["sourceRanges"] == ["10.20.0.0/24"]
    assert full["ingress-proxy"]["targetTags"] == ["ssc-proxy"]
    assert full["ingress-proxy"]["allows"] == [{"protocol": "tcp", "ports": ["3128"]}]
    assert full["egress-proxy"]["targetTags"] == ["ssc-proxy"]
    assert full["egress-data"]["targetTags"] == ["ssc-data"]


def test_the_database_is_a_zonal_shared_core_instance(cell_a: list[Declared]) -> None:
    settings = one(cell_a, "gcp:sql/databaseInstance:DatabaseInstance").inputs["settings"]
    assert (settings["tier"], settings["availabilityType"]) == ("db-f1-micro", "ZONAL")
    backups = settings["backupConfiguration"]
    assert backups["enabled"] is True
    assert backups["pointInTimeRecoveryEnabled"] is True


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


def test_the_gateway_is_request_billed_from_zero_with_an_hour_per_request(
    empty_b: list[Declared],
) -> None:
    gw = one(empty_b, "gcp:cloudrunv2/service:Service", "ssc-gateway").inputs
    assert gw["ingress"] == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    assert gw["template"]["scaling"]["minInstanceCount"] == 0
    assert gw["scaling"]["maxInstanceCount"] == 20
    assert gw["template"]["timeout"] == "3600s"
    assert gw["template"]["maxInstanceRequestConcurrency"] == 1000
    (container,) = gw["template"]["containers"]
    assert container["resources"] == {"cpuIdle": True, "limits": {"cpu": "1", "memory": "512Mi"}}
    assert gw["template"]["vpcAccess"]["egress"] == "ALL_TRAFFIC"
    (nic,) = gw["template"]["vpcAccess"]["networkInterfaces"]
    assert (nic["subnetwork"], nic["tags"]) == ("subnet-gateway-id", ["ssc-gateway"])


def test_no_internal_load_balancer_remains(cell_a: list[Declared]) -> None:
    kinds = {d.type for d in cell_a}
    regional = {k for k in kinds if "region" in k.lower() and "compute/" in k}
    assert regional == {NEG}
    assert {d.inputs["networkEndpointType"] for d in cell_a if d.type == NEG} == {"SERVERLESS"}
    assert "gcp:compute/forwardingRule:ForwardingRule" not in kinds
    schemes = {d.inputs.get("loadBalancingScheme") for d in cell_a if d.type in LB_KINDS}
    assert schemes == {"EXTERNAL_MANAGED"}


@pytest.mark.parametrize(
    ("config", "floor"),
    [
        ({"gateway_min": "2"}, 2),
        ({"warm": "true"}, 1),
        ({"warm": "true", "gateway_min": "3"}, 3),
        ({"gateway_min": "0"}, 0),
    ],
)
def test_gateway_min_and_warm_set_the_gateway_floor(config: dict[str, str], floor: int) -> None:
    declared = run(naming.cell_stack("testcell07"), config)
    gw = one(declared, "gcp:cloudrunv2/service:Service", "ssc-gateway").inputs
    assert gw["template"]["scaling"]["minInstanceCount"] == floor
    assert gw["template"]["containers"][0]["resources"]["cpuIdle"] is True


def test_the_cell_agent_scales_to_zero_with_a_pinned_ceiling(cell_a: list[Declared]) -> None:
    agent = one(cell_a, "gcp:cloudrunv2/service:Service", "ssc-cell-agent").inputs
    assert agent["template"]["scaling"]["minInstanceCount"] == 0
    assert agent["scaling"]["maxInstanceCount"] == cell.AGENT_MAX


def test_only_google_names_resolve_in_the_cell(cell_a: list[Declared]) -> None:
    policy = one(cell_a, "gcp:dns/responsePolicy:ResponsePolicy").inputs
    assert policy["networks"] == [{"networkUrl": "vpc-id"}]
    rules = {
        d.inputs["dnsName"]: d.inputs
        for d in cell_a
        if d.type == "gcp:dns/responsePolicyRule:ResponsePolicyRule"
    }
    sink = rules.pop(cell.SINKHOLE_NAME)["localData"]["localDatas"]
    assert {(d["type"], *d["rrdatas"]) for d in sink} == {
        ("A", cell.SINKHOLE),
        ("AAAA", cell.SINKHOLE_V6),
    }
    tlds = cell.tlds()
    assert len(tlds) > 1000 and {"com", "app", "io", "xn--p1ai"} <= set(tlds)
    assert "ssc-cell" not in tlds
    for tld in tlds:
        (answer,) = rules.pop(f"*.{tld}.")["localData"]["localDatas"]
        assert (answer["type"], answer["rrdatas"]) == ("CNAME", [cell.SINKHOLE_NAME])
    assert set(rules) == set(cell.GOOGLE_DNS_PASSTHRU)
    assert {r["behavior"] for r in rules.values()} == {"bypassResponsePolicy"}


def test_the_agent_runs_its_image_with_the_cell_wired_in() -> None:
    image = "us-central1-docker.pkg.dev/ssc-c-testcell05/ssc-platform/agent@sha256:" + "a" * 64
    declared = run(naming.cell_stack("testcell05"), {"agent_image": image})
    agent = one(declared, "gcp:cloudrunv2/service:Service", "ssc-cell-agent").inputs
    (container,) = agent["template"]["containers"]
    assert container["image"] == image
    env = {e["name"]: e["value"] for e in container["envs"]}
    assert env["SSC_CELL_PROJECT"] == "ssc-c-testcell05"
    assert env["SSC_IMAGE_REPOSITORY"] == (
        "us-central1-docker.pkg.dev/ssc-c-testcell05/ssc-apps/apps"
    )
    assert env["SSC_GATEWAY_SA"] == naming.sa_email("ssc-gateway", "ssc-c-testcell05")
    assert set(env) == AGENT_ENV
    subnet = one(declared, "gcp:compute/subnetworkIAMMember:SubnetworkIAMMember").inputs
    assert (subnet["subnetwork"], subnet["role"]) == ("apps", "roles/compute.networkUser")


def test_the_agent_runs_builds_as_ssc_build_once_both_images_are_set() -> None:
    image = "us-central1-docker.pkg.dev/ssc-c-testcell05/ssc-platform/agent@sha256:" + "a" * 64
    declared = run(naming.cell_stack("testcell05"), {"agent_image": image} | BUILD)
    agent = one(declared, "gcp:cloudrunv2/service:Service", "ssc-cell-agent").inputs
    env = {e["name"]: e["value"] for e in agent["template"]["containers"][0]["envs"]}
    assert set(env) == AGENT_ENV | BUILD_ENV
    assert env["SSC_BUILD_SA"] == naming.sa_email("ssc-build", "ssc-c-testcell05")
    assert (env["SSC_BUILD_TOOLS_IMAGE"], env["SSC_BUILD_FRONTEND_IMAGE"]) == (
        TOOLS_IMAGE,
        FRONTEND_IMAGE,
    )


@pytest.mark.parametrize(
    ("tools", "frontend"),
    [
        (TOOLS_IMAGE, None),
        (None, FRONTEND_IMAGE),
        (TOOLS_IMAGE, f"{naming.platform_registry()}/railpack-frontend:v0.40.1"),
        (TOOLS_IMAGE, "ghcr.io/railwayapp/railpack-frontend@sha256:" + "e" * 64),
        (f"{naming.platform_registry()}@sha256:" + "d" * 64, FRONTEND_IMAGE),
    ],
)
def test_build_images_are_both_set_in_the_platform_registry_and_pinned(
    tools: str | None, frontend: str | None
) -> None:
    with pytest.raises(ValueError, match="build_tools_image or build_frontend_image"):
        cell.build_images(tools, frontend)


def test_without_build_images_the_agent_sets_none_of_the_build_variables() -> None:
    assert cell.build_images(None, None) == (None, None)
    assert cell.build_images("", "") == (None, None)
    assert cell.build_images(TOOLS_IMAGE, FRONTEND_IMAGE) == (TOOLS_IMAGE, FRONTEND_IMAGE)


def test_without_an_agent_image_the_agent_is_a_placeholder(cell_a: list[Declared]) -> None:
    agent = one(cell_a, "gcp:cloudrunv2/service:Service", "ssc-cell-agent").inputs
    (container,) = agent["template"]["containers"]
    assert container["image"] == cell.PLACEHOLDER_IMAGE
    assert "envs" not in container


def test_the_probe_runner_stands_where_the_gateway_stands() -> None:
    digest = "sha256:" + "b" * 64
    declared = run(naming.cell_stack("testcell06"), {"probe": "true", "probe_digest": digest})
    job = one(declared, "gcp:cloudrunv2/job:Job").inputs["template"]["template"]
    assert job["serviceAccount"] == naming.sa_email("ssc-gateway", "ssc-c-testcell06")
    (nic,) = job["vpcAccess"]["networkInterfaces"]
    assert (nic["subnetwork"], nic["tags"]) == ("subnet-gateway-id", ["ssc-gateway"])
    (container,) = job["containers"]
    assert container["image"].endswith("/ssc-apps/apps@" + digest)
    env = {e["name"]: e["value"] for e in container["envs"]}
    number = project_number("ssc-c-testcell06")
    assert env["PROBE_URL"] == f"https://ssc-a-probe00000000000000a-{number}.us-central1.run.app"
    assert env["PROBE_PEER_URL"].startswith("https://ssc-a-probe00000000000000b-")
    nightly = f"serviceAccount:{mockcloud.NIGHTLY}"
    executor = one(declared, "gcp:cloudrunv2/jobIamMember:JobIamMember").inputs
    assert (executor["role"], executor["member"]) == ("roles/run.jobsExecutor", nightly)
    assert sorted(_grants(declared, nightly)) == ["roles/logging.viewer", "roles/run.viewer"]


def test_no_probe_runner_without_a_probe_digest(cell_a: list[Declared]) -> None:
    assert not [d for d in cell_a if d.type == "gcp:cloudrunv2/job:Job"]


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
    invoker = one(
        cell_a, "gcp:cloudrunv2/serviceIamMember:ServiceIamMember", "agent-invoker"
    ).inputs
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


def test_the_cell_agent_holds_only_what_the_driver_calls(cell_a: list[Declared]) -> None:
    agent = f"serviceAccount:{naming.sa_email('ssc-cell-agent', naming.cell_project(A))}"
    grants = _grants(cell_a, agent)
    secrets = grants["roles/secretmanager.admin"]
    assert secrets is not None
    assert '.startsWith("ssc-a-")' in secrets["expression"]
    assert not any("run." in role or "serviceAccount" in role for role in grants)
    roles = {
        d.inputs["roleId"]: d.inputs["permissions"]
        for d in cell_a
        if d.type == "gcp:projects/iAMCustomRole:IAMCustomRole"
    }
    assert all(p.endswith(".create") for p in roles["sscCellAgentCreate"])
    runtime = roles["sscCellAgentRuntime"]
    assert not any(p.endswith((".delete", ".create")) for p in runtime)
    assert "iam.serviceAccounts.actAs" in runtime
    assert {"run.services.setIamPolicy", "run.revisions.list"} <= set(runtime)
    assert "cloudbuild.builds.create" in roles["sscCellAgentCreate"]
    assert {p for p in runtime if p.startswith("cloudbuild.")} == {
        "cloudbuild.builds.get",
        "cloudbuild.builds.list",
    }


def test_app_images_are_read_by_the_agent_and_written_by_builds(cell_a: list[Declared]) -> None:
    grants = {
        d.name: (d.inputs["member"].split("@")[0], d.inputs["role"])
        for d in cell_a
        if d.type == "gcp:artifactregistry/repositoryIamMember:RepositoryIamMember"
    }
    assert grants == {
        "registry-build": ("serviceAccount:ssc-build", "roles/artifactregistry.writer"),
        "registry-build-tools": ("serviceAccount:ssc-build", "roles/artifactregistry.reader"),
        "registry-agent": ("serviceAccount:ssc-cell-agent", "roles/artifactregistry.reader"),
    }


def test_builds_read_the_platform_registry_and_push_to_the_cell_s_own(
    cell_a: list[Declared],
) -> None:
    kind = "gcp:artifactregistry/repositoryIamMember:RepositoryIamMember"
    tools = one(cell_a, kind, "registry-build-tools").inputs
    assert (tools["project"], tools["location"], tools["repository"]) == (
        naming.BOOTSTRAP_PROJECT,
        naming.REGION,
        naming.PLATFORM_REPOSITORY,
    )
    push = one(cell_a, kind, "registry-build").inputs
    assert (push["project"], push["role"]) == (
        naming.cell_project(A),
        "roles/artifactregistry.writer",
    )


def test_the_build_account_holds_no_storage_role(cell_a: list[Declared]) -> None:
    build = f"serviceAccount:{naming.sa_email('ssc-build', naming.cell_project(A))}"
    held = [
        (d.type, d.inputs["role"])
        for d in cell_a
        if d.inputs.get("member") == build or build in d.inputs.get("members", [])
    ]
    assert sorted(role for _, role in held) == [
        "roles/artifactregistry.reader",
        "roles/artifactregistry.writer",
        "roles/logging.logWriter",
    ]
    assert not [t for t, role in held if t.startswith("gcp:storage/") or "storage" in role]
    assert not [r for _, r in held if not r.startswith("roles/")]


def test_the_cell_deny_rule_names_every_ssc_identity(cell_a: list[Declared]) -> None:
    rule = one(cell_a, "gcp:iam/denyPolicy:DenyPolicy").inputs["rules"][0]["denyRule"]
    assert rule["deniedPermissions"] == [naming.SECRET_READ]
    denied = {p.rsplit("/", 1)[-1].split("@")[0] for p in rule["deniedPrincipals"]}
    assert denied == {
        "ssc-gateway",
        "ssc-cell-agent",
        "ssc-build",
        "ssc-data",
        naming.PROBE_DENIED_SA,
    }


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
    declared = run(naming.cell_stack("testcell04"), {"stage": stage, "database": "true"})
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


def test_by_default_a_cell_has_no_database_proxy_or_data_gateway(empty_b: list[Declared]) -> None:
    kinds = {d.type for d in empty_b}
    assert not {k for k in kinds if k.startswith("gcp:sql/")}
    assert "gcp:compute/instanceTemplate:InstanceTemplate" not in kinds
    assert "gcp:compute/instanceGroupManager:InstanceGroupManager" not in kinds
    services = {d.name for d in empty_b if d.type == "gcp:cloudrunv2/service:Service"}
    assert services == {"ssc-gateway", "ssc-cell-agent"}
    assert not _names(empty_b) & set().union(*naming.LAZY_RESOURCES.values())


@pytest.mark.parametrize("flag", sorted(naming.LAZY_RESOURCES))
def test_each_flag_adds_only_its_named_resources(flag: str, empty_b: list[Declared]) -> None:
    flagged = run(naming.cell_stack(B), EMPTY | {flag: "true"})
    assert _names(flagged) - _names(empty_b) == naming.LAZY_RESOURCES[flag]
    assert _names(empty_b) <= _names(flagged)
    flags = EMPTY_FLAGS | {flag: True}
    first = cell_diff.normalise(as_export(empty_b, B, EMPTY_FLAGS), B)
    second = cell_diff.normalise(as_export(flagged, B, flags), B)
    assert cell_diff.compare(first, second, [flag]) == []
    assert len(cell_diff.compare(first, second)) == len(naming.LAZY_RESOURCES[flag])


def test_the_lazy_resources_are_what_the_flags_say(cell_a: list[Declared]) -> None:
    template = one(cell_a, "gcp:compute/instanceTemplate:InstanceTemplate").inputs
    assert template["machineType"] == "e2-micro"
    assert template["tags"] == ["ssc-proxy"]
    (nic,) = template["networkInterfaces"]
    assert nic["networkIp"] == "10.20.4.10"
    assert nic["subnetwork"] == "subnet-gateway-id"
    assert "accessConfigs" not in nic
    assert "serviceAccount" not in template
    group = one(cell_a, "gcp:compute/instanceGroupManager:InstanceGroupManager").inputs
    assert group["targetSize"] == 1
    assert group["zone"].startswith(naming.REGION)
    assert group["updatePolicy"]["maxSurgeFixed"] == 0
    datagw = one(cell_a, "gcp:cloudrunv2/service:Service", naming.DATA_GATEWAY).inputs
    assert datagw["ingress"] == "INGRESS_TRAFFIC_INTERNAL_ONLY"
    assert datagw["template"]["serviceAccount"] == naming.sa_email("ssc-data", "ssc-c-testcell01")
    assert datagw["template"]["scaling"]["minInstanceCount"] == 0
    assert datagw["template"]["containers"][0]["resources"]["cpuIdle"] is True
    (nic,) = datagw["template"]["vpcAccess"]["networkInterfaces"]
    assert (nic["subnetwork"], nic["tags"]) == ("subnet-gateway-id", ["ssc-data"])


def test_a_full_and_an_empty_cell_differ_only_in_what_their_flags_name(
    cell_a: list[Declared], empty_b: list[Declared]
) -> None:
    full = as_export(cell_a, A, FULL_FLAGS)
    empty = as_export(empty_b, B, EMPTY_FLAGS)
    differ = cell_diff.differing(cell_diff.flags(full), cell_diff.flags(empty))
    assert differ == ["database", "egress", "connections"]
    first, second = cell_diff.normalise(full, A), cell_diff.normalise(empty, B)
    assert cell_diff.compare(first, second, differ) == []
    assert cell_diff.compare(first, second)


def test_a_stack_without_a_flags_output_has_the_defaults(cell_a: list[Declared]) -> None:
    assert cell_diff.flags(as_export(cell_a, A)) == EMPTY_FLAGS


def test_a_differing_flag_hides_nothing_else(
    cell_a: list[Declared], empty_b: list[Declared]
) -> None:
    drifted = [
        Declared(d.type, d.name, {**d.inputs, "machineType": "e2-small"}, d.outputs)
        if d.name == "proxy-template"
        else Declared(d.type, d.name, {**d.inputs, "versioning": {"enabled": False}}, d.outputs)
        if d.type == "gcp:storage/bucket:Bucket"
        else d
        for d in cell_a
    ]
    first = cell_diff.normalise(as_export(drifted, A, FULL_FLAGS), A)
    second = cell_diff.normalise(as_export(empty_b, B, EMPTY_FLAGS), B)
    diffs = cell_diff.compare(first, second, ["database", "egress", "connections"])
    assert [d.split(" ")[0] for d in diffs] == ["gcp:storage/bucket:Bucket::bucket"]


def test_gateway_min_and_warm_differ_only_in_the_gateway_floor(empty_b: list[Declared]) -> None:
    warm = run(naming.cell_stack(B), EMPTY | {"warm": "true", "gateway_min": "2"})
    flags = EMPTY_FLAGS | {"warm": True, "gateway_min": 2}
    first = cell_diff.normalise(as_export(empty_b, B, EMPTY_FLAGS), B)
    second = cell_diff.normalise(as_export(warm, B, flags), B)
    differ = cell_diff.differing(EMPTY_FLAGS, flags)
    assert differ == ["gateway_min", "warm"]
    assert cell_diff.compare(first, second, differ) == []
    assert {d.split(": ")[0] for d in cell_diff.compare(first, second)} == {
        f"{naming.GATEWAY_SERVICE} {side}.{naming.GATEWAY_MIN_PATH}" for side in ("in", "out")
    }


def test_the_budget_alert_is_on_the_project_and_its_billing_account() -> None:
    declared = run(naming.cell_stack("testcell08"), {"billing_account": "000000-111111-222222"})
    project = one(declared, "gcp:organizations/project:Project").inputs
    assert project["billingAccount"] == "000000-111111-222222"
    budget = one(declared, "gcp:billing/budget:Budget").inputs
    assert budget["billingAccount"] == "000000-111111-222222"
    number = project_number("ssc-c-testcell08")
    assert budget["budgetFilter"]["projects"] == [f"projects/{number}"]
    assert budget["amount"]["specifiedAmount"]["units"] == str(cell.CELL_BUDGET_USD)


def test_the_billing_account_defaults_to_ours(empty_b: list[Declared]) -> None:
    project = one(empty_b, "gcp:organizations/project:Project").inputs
    assert project["billingAccount"] == naming.BILLING_ACCOUNT


@pytest.fixture(scope="module")
def bare() -> list[Declared]:
    return run(naming.cell_stack("testcell09"))


def _entry(declared: list[Declared]) -> dict[str, Declared]:
    return {k: d for d in declared if (k := f"{d.type}::{d.name}") in ENTRY_RESOURCES}


def _auth_data(declared: list[Declared]) -> str:
    auth = one(declared, "gcp:certificatemanager/dnsAuthorization:DnsAuthorization")
    return auth.outputs["dnsResourceRecords"][0]["data"]


def test_the_public_entry_exists_at_onboarding_with_no_flags(bare: list[Declared]) -> None:
    assert set(_entry(bare)) == ENTRY_RESOURCES
    assert not ENTRY_RESOURCES & set().union(*naming.LAZY_RESOURCES.values())


def test_one_https_rule_and_a_redirect_on_the_same_address(bare: list[Declared]) -> None:
    address = entry_address("ssc-c-testcell09")
    ip = one(bare, "gcp:compute/globalAddress:GlobalAddress", "entry-ip").inputs
    assert (ip["addressType"], ip["ipVersion"]) == ("EXTERNAL", "IPV4")
    rules = {
        d.inputs["portRange"]: d.inputs
        for d in bare
        if d.type == "gcp:compute/globalForwardingRule:GlobalForwardingRule"
    }
    assert set(rules) == {"443", "80"}
    assert {r["ipAddress"] for r in rules.values()} == {address}
    assert rules["443"]["target"] == "entry-https-id"
    assert rules["80"]["target"] == "entry-http-id"
    http = one(bare, "gcp:compute/targetHttpProxy:TargetHttpProxy").inputs
    assert http["urlMap"] == "entry-redirect-id"
    redirect = one(bare, "gcp:compute/uRLMap:URLMap", "entry-redirect").inputs
    assert redirect["defaultUrlRedirect"]["httpsRedirect"] is True
    assert "defaultService" not in redirect


@pytest.mark.parametrize(
    ("resource", "service"), [("gateway", naming.GATEWAY), ("agent", naming.CELL_AGENT)]
)
def test_each_backend_reaches_its_service_through_a_serverless_neg(
    bare: list[Declared], resource: str, service: str
) -> None:
    neg = one(bare, NEG, f"{resource}-neg").inputs
    assert neg["networkEndpointType"] == "SERVERLESS"
    assert neg["cloudRun"] == {"service": service}
    assert neg["region"] == naming.REGION
    backend = one(bare, BACKEND, f"{resource}-backend").inputs
    assert backend["backends"] == [{"group": f"{resource}-neg-id"}]
    assert backend["loadBalancingScheme"] == "EXTERNAL_MANAGED"
    assert "healthChecks" not in backend
    assert one(bare, "gcp:compute/uRLMap:URLMap", "entry-map").inputs["defaultService"] == (
        "gateway-backend-id"
    )


def test_a_request_through_the_load_balancer_may_last_3600_seconds(bare: list[Declared]) -> None:
    """A serverless NEG's backend timeout is fixed at 3600 s and Google refuses ``timeoutSec``
    on it, so the stack must leave it unset and the gateway's own timeout must match."""
    assert not [d for d in bare if d.type == BACKEND and "timeoutSec" in d.inputs]
    assert cell.ENTRY_TIMEOUT_SECONDS == 3600
    gw = one(bare, "gcp:cloudrunv2/service:Service", naming.GATEWAY).inputs
    assert gw["template"]["timeout"] == f"{cell.ENTRY_TIMEOUT_SECONDS}s"


def test_the_certificate_and_dns_records_follow_the_label(cell_a: list[Declared]) -> None:
    wildcard = "*.testcell01.delimitusapps.com"
    auth = one(cell_a, "gcp:certificatemanager/dnsAuthorization:DnsAuthorization").inputs
    assert auth["domain"] == "testcell01.delimitusapps.com"
    cert = one(cell_a, "gcp:certificatemanager/certificate:Certificate").inputs
    assert cert["managed"] == {"domains": [wildcard], "dnsAuthorizations": ["cert-dns-auth-id"]}
    entry = one(cell_a, "gcp:certificatemanager/certificateMapEntry:CertificateMapEntry").inputs
    assert (entry["map"], entry["hostname"], entry["certificates"]) == (
        "ssc-entry",
        wildcard,
        ["cert-id"],
    )
    proxy = one(cell_a, "gcp:compute/targetHttpsProxy:TargetHttpsProxy").inputs
    assert proxy["certificateMap"] == "//certificatemanager.googleapis.com/cert-map-id"
    assert "sslCertificates" not in proxy
    records = {
        d.name: d.inputs
        for d in cell_a
        if d.type == RECORD and d.inputs["project"] == naming.BOOTSTRAP_PROJECT
    }
    assert set(records) == {"dns-wildcard", "dns-cert-auth"}
    for record in records.values():
        assert (record["project"], record["managedZone"]) == ("ssc-platform-0", "delimitusapps")
    assert (records["dns-wildcard"]["name"], records["dns-wildcard"]["type"]) == (
        f"{wildcard}.",
        "A",
    )
    assert records["dns-wildcard"]["rrdatas"] == [entry_address(naming.cell_project(A))]
    assert (records["dns-cert-auth"]["name"], records["dns-cert-auth"]["type"]) == (
        "_acme-challenge.testcell01.delimitusapps.com.",
        "CNAME",
    )
    assert records["dns-cert-auth"]["rrdatas"] == [_auth_data(cell_a)]


def test_no_cloud_armor(cell_a: list[Declared]) -> None:
    assert not [d for d in cell_a if d.type.startswith("gcp:compute/securityPolicy")]
    for backend in (d.inputs for d in cell_a if d.type == BACKEND):
        assert "securityPolicy" not in backend and "edgeSecurityPolicy" not in backend


def test_a_second_label_changes_only_label_derived_values(
    cell_a: list[Declared], cell_b: list[Declared]
) -> None:
    first, second = _entry(cell_a), _entry(cell_b)
    assert set(first) == set(second) == ENTRY_RESOURCES
    swaps = (
        (A, B),
        (entry_address(naming.cell_project(A)), entry_address(naming.cell_project(B))),
        (_auth_data(cell_a), _auth_data(cell_b)),
        (project_number(naming.cell_project(A)), project_number(naming.cell_project(B))),
    )
    changed = 0
    for key, d in first.items():
        text = json.dumps(d.inputs, sort_keys=True)
        moved = text
        for old, new in swaps:
            moved = moved.replace(old, new)
        assert moved == json.dumps(second[key].inputs, sort_keys=True), key
        changed += text != moved
    assert changed >= 10


def test_the_diff_shows_a_record_pointing_at_another_cell(
    cell_a: list[Declared], cell_b: list[Declared]
) -> None:
    stolen = entry_address(naming.cell_project(A))
    drifted = [
        Declared(d.type, d.name, {**d.inputs, "rrdatas": [stolen]}, d.outputs)
        if d.name == "dns-wildcard"
        else d
        for d in cell_b
    ]
    first = cell_diff.normalise(as_export(cell_a, A), A)
    second = cell_diff.normalise(as_export(drifted, B), B)
    diffs = cell_diff.compare(first, second)
    assert [d.split(" ")[:2] for d in diffs] == [[f"{RECORD}::dns-wildcard", "in.rrdatas[0]:"]]


def test_the_diff_hides_certificate_progress_but_not_its_state(
    cell_a: list[Declared], cell_b: list[Declared]
) -> None:
    def issued(declared: list[Declared], state: str, attempt: str) -> list[Declared]:
        progress = {
            "state": state,
            "authorizationAttemptInfos": [{"state": attempt, "details": attempt}],
            "provisioningIssues": [{"reason": attempt}],
        }
        return [
            Declared(d.type, d.name, d.inputs, {**d.outputs, "managed": progress})
            if d.type == "gcp:certificatemanager/certificate:Certificate"
            else d
            for d in declared
        ]

    first = cell_diff.normalise(as_export(issued(cell_a, "ACTIVE", "AUTHORIZED"), A), A)
    second = cell_diff.normalise(as_export(issued(cell_b, "ACTIVE", "AUTHORIZING"), B), B)
    assert cell_diff.compare(first, second) == []
    failed = cell_diff.normalise(as_export(issued(cell_b, "FAILED", "AUTHORIZED"), B), B)
    assert [d.split(" ")[1] for d in cell_diff.compare(first, failed)] == ["out.managed.state:"]


def test_the_gateway_is_public_only_through_the_load_balancer(bare: list[Declared]) -> None:
    gw = one(bare, "gcp:cloudrunv2/service:Service", naming.GATEWAY).inputs
    assert gw["ingress"] == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    assert gw["ingress"] != "INGRESS_TRAFFIC_ALL"
    invoker = one(bare, "gcp:cloudrunv2/serviceIamMember:ServiceIamMember", "gateway-invoker")
    assert (invoker.inputs["name"], invoker.inputs["role"], invoker.inputs["member"]) == (
        naming.GATEWAY,
        "roles/run.invoker",
        "allUsers",
    )
    public = {
        d.inputs["name"]
        for d in bare
        if d.type.endswith("IamMember") and d.inputs.get("member") == "allUsers"
    }
    assert public == {naming.GATEWAY}


def test_the_public_invoker_tag_goes_on_the_gateway_alone_before_its_grant(
    monkeypatch: pytest.MonkeyPatch, bare: list[Declared]
) -> None:
    binding = one(bare, TAG_BINDING).inputs
    number = project_number("ssc-c-testcell09")
    assert binding["parent"] == (
        f"//run.googleapis.com/projects/{number}/locations/{naming.REGION}/services/"
        f"{naming.GATEWAY}"
    )
    assert binding["tagValue"] == mockcloud.PUBLIC_TAG
    assert binding["location"] == naming.REGION
    invoker = _options(monkeypatch)[
        "gcp:cloudrunv2/serviceIamMember:ServiceIamMember::gateway-invoker"
    ]
    after = {d._name for d in cast(list[pulumi.Resource], invoker.depends_on or [])}  # pyright: ignore[reportPrivateUsage]
    assert "gateway-public-tag" in after


def test_the_agent_host_is_routed_to_the_agent_alone(bare: list[Declared]) -> None:
    assert check_apps_domain(naming.APPS_DOMAIN) == "delimitusapps.com"
    assert slug_problem(naming.AGENT_HOST_LABEL) == "double_dash"
    host = naming.agent_host("testcell09")
    assert host == "ssc--agent.testcell09.delimitusapps.com"
    assert parse_app_host(host, naming.APPS_DOMAIN) is None
    assert host.split(".", 1)[1] == naming.cell_wildcard("testcell09").removeprefix("*.")
    url_map = one(bare, "gcp:compute/uRLMap:URLMap", "entry-map").inputs
    assert url_map["hostRules"] == [{"hosts": [host], "pathMatcher": "agent"}]
    assert url_map["pathMatchers"] == [{"name": "agent", "defaultService": "agent-backend-id"}]
    assert url_map["defaultService"] == "gateway-backend-id"
    text = json.dumps(url_map)
    assert text.count("agent-backend-id") == 1
    assert text.count("gateway-backend-id") == 1


def test_the_agent_is_internal_and_load_balancer_with_the_host_as_audience(
    bare: list[Declared],
) -> None:
    agent = one(bare, "gcp:cloudrunv2/service:Service", naming.CELL_AGENT).inputs
    assert agent["ingress"] == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    assert agent["customAudiences"] == ["https://ssc--agent.testcell09.delimitusapps.com"]
    assert naming.agent_url("testcell09") == agent["customAudiences"][0]
    invokers = [
        d.inputs["member"]
        for d in bare
        if d.type == "gcp:cloudrunv2/serviceIamMember:ServiceIamMember"
        and d.inputs["name"] == naming.CELL_AGENT
    ]
    assert invokers == [f"serviceAccount:{mockcloud.CONTROL['staging']}"]
    gateway = one(bare, "gcp:cloudrunv2/service:Service", naming.GATEWAY).inputs
    assert "customAudiences" not in gateway


def test_the_https_proxy_refuses_anything_below_tls_1_2(bare: list[Declared]) -> None:
    tls = one(bare, "gcp:compute/sSLPolicy:SSLPolicy").inputs
    assert (tls["name"], tls["minTlsVersion"], tls["profile"]) == ("ssc-entry", "TLS_1_2", "MODERN")
    proxy = one(bare, "gcp:compute/targetHttpsProxy:TargetHttpsProxy").inputs
    assert proxy["sslPolicy"] == "entry-tls-id"


def test_the_stack_exports_its_public_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    exported: dict[str, Any] = {}
    monkeypatch.setattr(pulumi, "export", lambda name, value: exported.__setitem__(name, value))
    run(naming.cell_stack("testcell10"))
    assert {"entry_address", "public_host_suffix", "certificate_id", "agent_host"} <= set(exported)
    assert exported["public_host_suffix"] == "testcell10.delimitusapps.com"
    assert exported["agent_host"] == "ssc--agent.testcell10.delimitusapps.com"
    assert exported["agent_url"] == "https://ssc--agent.testcell10.delimitusapps.com"
