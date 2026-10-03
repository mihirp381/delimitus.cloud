"""SSC-095: a cell stack checked against the folder's policy table. Real refusal happens only at
apply; these plant one violation per policy in a full cell and show the same table catches it."""

import json
from collections.abc import Callable
from typing import Any

import pytest

from mockcloud import FOLDERS, PUBLIC_TAG, Declared, as_export, run
from ssc_infra import cell_diff, naming, policies
from ssc_infra.run import CommandError

RULES = policies.cell_rules(naming.OPERATOR)
FULL = {
    "probe": "true",
    "probe_digest": "sha256:" + "c" * 64,
    "database": "true",
    "egress": "true",
    "connections": "true",
    "warm": "true",
    "gateway_min": "1",
}
A, B = "testcell11", "testcell12"
OTHER_CELL = naming.cell_project(B)
PLATFORM = {"stage_folder_ids": FOLDERS, "cell_policies": policies.summaries(RULES)}

type Plant = Callable[[list[Declared]], list[Declared]]


@pytest.fixture(scope="module")
def full() -> list[Declared]:
    return run(naming.cell_stack(A), FULL)


@pytest.fixture(scope="module")
def full_b() -> list[Declared]:
    return run(naming.cell_stack(B), FULL)


def _check(declared: list[Declared]) -> dict[str, list[str]]:
    return policies.violations(RULES, ((d.type, d.name, d.inputs) for d in declared), PUBLIC_TAG)


def _add(type_: str, resource: str, /, **inputs: Any) -> Plant:
    return lambda declared: [*declared, Declared(type_, resource, inputs, {})]


def _change(resource: str, /, **inputs: Any) -> Plant:
    return lambda declared: [
        Declared(d.type, d.name, {**d.inputs, **inputs}, d.outputs) if d.name == resource else d
        for d in declared
    ]


def _tag(service: str) -> str:
    number = "100000000000"
    return f"//run.googleapis.com/projects/{number}/locations/{naming.REGION}/services/{service}"


PLANTED: dict[str, tuple[str, Plant]] = {
    "public-bucket": (
        "storage.publicAccessPrevention",
        _add(
            "gcp:storage/bucketIAMMember:BucketIAMMember",
            "bucket-public",
            bucket=f"{naming.cell_project(A)}-cell",
            role="roles/storage.objectViewer",
            member="allUsers",
        ),
    ),
    "bucket-prevention-off": (
        "storage.publicAccessPrevention",
        _change("bucket", publicAccessPrevention="inherited"),
    ),
    "outside-member": (
        "iam.managed.allowedPolicyMembers",
        _add(
            "gcp:projects/iAMMember:IAMMember",
            "outsider",
            project=naming.cell_project(A),
            role="roles/viewer",
            member="user:someone@example.com",
        ),
    ),
    "public-agent": (
        "iam.managed.allowedPolicyMembers",
        _add(
            "gcp:cloudrunv2/serviceIamMember:ServiceIamMember",
            "agent-public",
            name=naming.CELL_AGENT,
            role="roles/run.invoker",
            member="allUsers",
        ),
    ),
    "tag-on-the-agent": (
        "iam.managed.allowedPolicyMembers",
        _add(
            "gcp:tags/locationTagBinding:LocationTagBinding",
            "agent-public-tag",
            parent=_tag(naming.CELL_AGENT),
            tagValue=PUBLIC_TAG,
            location=naming.REGION,
        ),
    ),
    "tag-on-the-project": (
        "iam.managed.allowedPolicyMembers",
        _add(
            "gcp:tags/tagBinding:TagBinding",
            "project-public-tag",
            parent="//cloudresourcemanager.googleapis.com/projects/100000000000",
            tagValue=PUBLIC_TAG,
        ),
    ),
    "sa-key": (
        "iam.disableServiceAccountKeyCreation",
        _add("gcp:serviceaccount/key:Key", "build-key", serviceAccountId="ssc-build"),
    ),
    "peer-another-cell": (
        "compute.restrictVpcPeering",
        _add(
            "gcp:compute/networkPeering:NetworkPeering",
            "peer",
            network="vpc-id",
            peerNetwork=f"projects/{OTHER_CELL}/global/networks/ssc-cell",
        ),
    ),
    "shared-vpc-service": (
        "compute.restrictSharedVpcHostProjects",
        _add(
            "gcp:compute/sharedVPCServiceProject:SharedVPCServiceProject",
            "join-host",
            hostProject=OTHER_CELL,
            serviceProject=naming.cell_project(A),
        ),
    ),
    "shared-vpc-host": (
        "compute.restrictSharedVpcHostProjects",
        _add(
            "gcp:compute/sharedVPCHostProject:SharedVPCHostProject",
            "become-host",
            project=naming.cell_project(A),
        ),
    ),
    "agent-ingress-all": (
        "run.allowedIngress",
        _change(naming.CELL_AGENT, ingress="INGRESS_TRAFFIC_ALL"),
    ),
    "proxy-external-ip": (
        "compute.vmExternalIpAccess",
        lambda declared: [
            Declared(
                d.type,
                d.name,
                {
                    **d.inputs,
                    "networkInterfaces": [
                        {**nic, "accessConfigs": [{}]} for nic in d.inputs["networkInterfaces"]
                    ],
                },
                d.outputs,
            )
            if d.name == "proxy-template"
            else d
            for d in declared
        ],
    ),
    "sql-public-ip": (
        "sql.restrictPublicIp",
        lambda declared: [
            Declared(
                d.type,
                d.name,
                {
                    **d.inputs,
                    "settings": {
                        **d.inputs["settings"],
                        "ipConfiguration": {
                            **d.inputs["settings"]["ipConfiguration"],
                            "ipv4Enabled": True,
                        },
                    },
                },
                d.outputs,
            )
            if d.name == "sql"
            else d
            for d in declared
        ],
    ),
    "bucket-outside-the-region": (
        "gcp.resourceLocations",
        _change("bucket", location="EU"),
    ),
}


def test_the_cell_stack_breaks_no_policy(full: list[Declared]) -> None:
    assert len(full) > 80
    assert _check(full) == {}


def test_every_policy_has_a_planted_violation() -> None:
    assert {constraint for constraint, _ in PLANTED.values()} == {r.constraint for r in RULES}
    assert set(policies.CHECKS) == {r.constraint for r in RULES}


@pytest.mark.parametrize("case", sorted(PLANTED))
def test_a_planted_violation_is_refused(full: list[Declared], case: str) -> None:
    constraint, plant = PLANTED[case]
    found = _check(plant(full))
    assert constraint in found, found
    assert len(found[constraint]) == 1


def test_the_gateway_s_public_invoker_needs_the_tag(full: list[Declared]) -> None:
    untagged = [d for d in full if d.type != "gcp:tags/locationTagBinding:LocationTagBinding"]
    found = _check(untagged)
    assert list(found) == ["iam.managed.allowedPolicyMembers"]
    assert found["iam.managed.allowedPolicyMembers"] == [
        "gcp:cloudrunv2/serviceIamMember:ServiceIamMember::gateway-invoker grants allUsers "
        "outside the tagged gateway"
    ]


def test_another_tag_value_is_no_exception(full: list[Declared]) -> None:
    found = policies.violations(RULES, ((d.type, d.name, d.inputs) for d in full), "tagValues/1")
    assert list(found) == ["iam.managed.allowedPolicyMembers"]


def test_the_nat_address_and_the_entry_address_are_not_vm_addresses(
    full: list[Declared],
) -> None:
    external = {
        d.name
        for d in full
        if d.type.startswith("gcp:compute/") and d.inputs.get("addressType") == "EXTERNAL"
    }
    assert external == {"nat-ip-gateway", "entry-ip"}
    assert "compute.vmExternalIpAccess" not in _check(full)


def test_the_cloud_sql_peering_is_google_service_networking(full: list[Declared]) -> None:
    connection = next(d for d in full if d.type == "gcp:servicenetworking/connection:Connection")
    assert connection.inputs["service"] == "servicenetworking.googleapis.com"
    rerouted = _change("psa", service="example.com")(full)
    assert list(_check(rerouted)) == ["compute.restrictVpcPeering"]


class FakeCloud:
    """``pulumi`` reads the stacks from ``states``; ``gcloud_json`` answers from ``set_on``."""

    def __init__(self, states: dict[str, Any], set_on: dict[str, list[str]]) -> None:
        self.states = states
        self.set_on = set_on
        self.gcloud: list[tuple[str, ...]] = []

    def pulumi(self, *args: str, cwd: str | None = None) -> str:
        match args:
            case ("stack", "export", "--stack", stack):
                return json.dumps(self.states[stack])
            case ("stack", "output", "--stack", naming.PLATFORM_STACK, "--json"):
                return json.dumps(PLATFORM)
            case _:
                raise AssertionError(args)

    def gcloud_json(self, *args: str) -> Any:
        self.gcloud.append(args)
        project = args[2].removeprefix("--project=")
        if project not in self.set_on:
            raise CommandError("gcloud org-policies list failed: PERMISSION_DENIED")
        return [{"constraint": c, "listPolicy": "SET"} for c in self.set_on[project]]


@pytest.fixture
def cloud(
    full: list[Declared], full_b: list[Declared], monkeypatch: pytest.MonkeyPatch
) -> FakeCloud:
    states = {naming.cell_stack(A): as_export(full, A), naming.cell_stack(B): as_export(full_b, B)}
    fake = FakeCloud(states, {naming.cell_project(A): [], naming.cell_project(B): []})
    monkeypatch.setattr(cell_diff, "pulumi", fake.pulumi)
    monkeypatch.setattr(cell_diff, "gcloud_json", fake.gcloud_json)
    return fake


def test_cell_diff_lists_the_policies_in_force_with_one_read_per_cell(
    cloud: FakeCloud, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cell_diff.main([A, B]) == 0
    out = capsys.readouterr().out
    for label in (A, B):
        assert f"policies in force on {naming.cell_project(label)}:" in out
    for rule in RULES:
        assert out.count(f"  {rule.constraint}: {rule.summary()}\n") == 2
    assert "override" not in out
    assert cloud.gcloud == [
        (
            "org-policies",
            "list",
            f"--project={naming.cell_project(label)}",
            f"--billing-project={naming.BOOTSTRAP_PROJECT}",
        )
        for label in (A, B)
    ]


def test_cell_diff_fails_on_a_policy_set_on_the_project(
    cloud: FakeCloud, capsys: pytest.CaptureFixture[str]
) -> None:
    cloud.set_on[naming.cell_project(B)] = ["iam.disableServiceAccountKeyCreation"]
    assert cell_diff.main([A, B]) == 1
    out = capsys.readouterr().out
    assert out.count("override:") == 1
    assert "  override: set on the project: iam.disableServiceAccountKeyCreation" in out


def test_cell_diff_fails_when_the_project_policies_cannot_be_read(cloud: FakeCloud) -> None:
    del cloud.set_on[naming.cell_project(A)]
    assert cell_diff.main([A, B]) == 1


def test_a_cell_outside_the_stage_folders_or_with_its_own_policy_is_an_override(
    full: list[Declared],
) -> None:
    moved = _change("project", folderId="folders/333333333333")(full)
    overridden = _add(
        "gcp:orgpolicy/policy:Policy",
        "loosen",
        name=f"projects/{naming.cell_project(A)}/policies/gcp.resourceLocations",
    )(full)
    assert cell_diff.in_force(PLATFORM, as_export(full, A), [])[1] == []
    assert cell_diff.in_force(PLATFORM, as_export(moved, A), [])[1] == [
        "project in folder folders/333333333333, not a stage folder"
    ]
    assert cell_diff.in_force(PLATFORM, as_export(overridden, A), [])[1] == [
        "the cell stack declares gcp:orgpolicy/policy:Policy::loosen"
    ]
    assert cell_diff.in_force({}, as_export(full, A), [])[1] == [
        "the platform stack exports no cell policies",
        "project in folder 222222222222, not a stage folder",
    ]
