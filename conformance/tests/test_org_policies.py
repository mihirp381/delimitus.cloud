"""The organisation-policy check against a scripted folder: one planted difference each."""

import copy
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from ssc_conformance import cloud_read as cloud
from ssc_conformance import evidence as ev
from ssc_conformance import matrix
from ssc_conformance import org_policies as op

PROJECT = "ssc-c-test"
FOLDER = "111"
OPERATOR = "user:operator@example.com"
EXPECTED_ENV = {
    op.TAG_KEY_ENV: "tagKeys/1",
    op.TAG_VALUE_ENV: "tagValues/2",
    op.ORG_ENV: "000000000000",
    op.OPERATOR_ENV: OPERATOR,
}
EXPECTED = op.load_expected(EXPECTED_ENV)
TAG = "resource.matchTagId('tagKeys/1', 'tagValues/2')"

type Json = dict[str, Any]


def live(constraint: str, spec: Json) -> Json:
    """A policy as the Organization Policy API returns it for an expected ``spec``."""
    if spec["mode"] == "deny_all":
        rules: list[Json] = [{"denyAll": True}]
    elif spec["mode"] == "allow":
        rules = [{"values": {"allowedValues": list(reversed(spec["values"]))}}]
    else:
        main: Json = {"enforce": True}
        if spec["parameters"]:
            main["parameters"] = {k: list(v) for k, v in spec["parameters"].items()}
        rules = [main]
    if spec["exception"]:
        rules.append({"enforce": False, "condition": {"expression": spec["exception"]}})
    return {"name": f"folders/{FOLDER}/policies/{constraint}", "spec": {"rules": rules}}


def folder_policies() -> list[Json]:
    return [live(c, s) for c, s in EXPECTED.items()]


def transport(
    folder: list[Json], project: list[Json], *, refused: bool = False, pages: bool = False
) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if refused:
            return httpx2.Response(403)
        found = folder if f"/folders/{FOLDER}/" in str(request.url) else project
        if pages and "pageToken" not in str(request.url):
            return httpx2.Response(200, json={"policies": found[:3], "nextPageToken": "next"})
        return httpx2.Response(200, json={"policies": found[3:] if pages else found})

    return httpx2.MockTransport(handle)


async def run(
    folder: list[Json], project: list[Json] | None = None, **kw: bool
) -> tuple[ev.Result, str]:
    async def token() -> str:
        return "token"

    client = httpx2.AsyncClient(transport=transport(folder, project or [], **kw))
    reader = cloud.CloudReader(token, client=client)
    try:
        return await op.check(reader, PROJECT, FOLDER, expected=EXPECTED)
    finally:
        await reader.aclose()


async def test_the_folder_as_declared_passes_and_is_listed() -> None:
    result, text = await run(folder_policies())
    assert result == ev.Result(matrix.ORG_POLICIES, ev.OK, "")
    assert text.count("\n") == len(EXPECTED) + 2
    assert "| gcp.resourceLocations | allow global, in:us-central1-locations |" in text
    assert "| compute.restrictSharedVpcHostProjects | deny all |" in text
    assert "allowedMemberSubjects user:operator@example.com" in text
    assert f"off where {TAG}" in text


async def test_policies_come_back_in_pages() -> None:
    result, _ = await run(folder_policies(), pages=True)
    assert result.status == ev.OK


def _loosened(constraint: str, change: Any) -> list[Json]:
    policies = folder_policies()
    for p in policies:
        if p["name"].endswith(f"/{constraint}"):
            change(p["spec"]["rules"])
    return policies


@pytest.mark.parametrize(
    ("constraint", "change", "named"),
    [
        (
            "storage.publicAccessPrevention",
            lambda r: r[0].update(enforce=False),
            "storage.publicAccessPrevention: expected enforced",
        ),
        (
            "gcp.resourceLocations",
            lambda r: r[0]["values"]["allowedValues"].append("us-east1"),
            "found allow global, in:us-central1-locations, us-east1",
        ),
        (
            "compute.vmExternalIpAccess",
            lambda r: r.__setitem__(0, {"allowAll": True}),
            "compute.vmExternalIpAccess",
        ),
        ("iam.managed.allowedPolicyMembers", lambda r: r.pop(), "expected enforced"),
        (
            "iam.managed.allowedPolicyMembers",
            lambda r: r[0]["parameters"].update(allowedPrincipalSets=[]),
            "allowedPrincipalSets",
        ),
        (
            "iam.managed.allowedPolicyMembers",
            lambda r: r[0]["parameters"]["allowedMemberSubjects"].append("user:b@example.com"),
            "allowedMemberSubjects user:b@example.com, user:operator@example.com",
        ),
        (
            "run.allowedIngress",
            lambda r: r[0]["values"]["allowedValues"].append("is:all"),
            "is:all",
        ),
    ],
)
async def test_a_loosened_policy_fails_naming_it(constraint: str, change: Any, named: str) -> None:
    result, _ = await run(_loosened(constraint, change))
    assert result.status == ev.FAIL
    assert constraint in result.reason
    assert named in result.reason


async def test_an_exception_on_another_tag_fails() -> None:
    other = "resource.matchTagId('tagKeys/1', 'tagValues/3')"
    result, _ = await run(
        _loosened(
            "iam.managed.allowedPolicyMembers", lambda r: r[1]["condition"].update(expression=other)
        )
    )
    assert result.status == ev.FAIL
    assert "expected enforced; allowedMemberSubjects user:operator@example.com" in result.reason
    assert f"off where {other}" in result.reason


async def test_a_second_exception_fails() -> None:
    def add(rules: list[Json]) -> None:
        rules.append({"enforce": False, "condition": {"expression": "true"}})

    result, _ = await run(_loosened("iam.managed.allowedPolicyMembers", add))
    assert result.status == ev.FAIL
    assert "unrecognised (2 conditional rules)" in result.reason


async def test_a_policy_inheriting_from_its_parent_fails() -> None:
    policies = folder_policies()
    for p in policies:
        if p["name"].endswith("/gcp.resourceLocations"):
            p["spec"]["inheritFromParent"] = True
    result, _ = await run(policies)
    assert result.status == ev.FAIL
    assert result.reason.startswith("gcp.resourceLocations: expected allow")
    assert result.reason.endswith("; inherits from the parent")


def test_the_tag_comes_from_the_environment() -> None:
    expression = EXPECTED["iam.managed.allowedPolicyMembers"]["exception"]
    assert expression == TAG
    raw = json.loads(op.EXPECTED.read_text(encoding="utf-8"))
    assert raw["iam.managed.allowedPolicyMembers"]["exception"] == (
        "resource.matchTagId('${SSC_PUBLIC_TAG_KEY}', '${SSC_PUBLIC_TAG_VALUE}')"
    )


def test_the_org_comes_from_the_environment() -> None:
    sets = EXPECTED["iam.managed.allowedPolicyMembers"]["parameters"]["allowedPrincipalSets"]
    assert sets == ["//cloudresourcemanager.googleapis.com/organizations/000000000000"]
    raw = json.loads(op.EXPECTED.read_text(encoding="utf-8"))
    assert raw["iam.managed.allowedPolicyMembers"]["parameters"]["allowedPrincipalSets"] == [
        "//cloudresourcemanager.googleapis.com/organizations/${SSC_ORG_ID}"
    ]


@pytest.mark.parametrize(
    ("environ", "named"),
    [
        ({}, "SSC_PUBLIC_TAG_KEY, SSC_PUBLIC_TAG_VALUE, SSC_ORG_ID"),
        ({"SSC_PUBLIC_TAG_KEY": "tagKeys/1"}, "SSC_PUBLIC_TAG_VALUE"),
        (EXPECTED_ENV | {"SSC_PUBLIC_TAG_VALUE": "2"}, "SSC_PUBLIC_TAG_VALUE"),
        (EXPECTED_ENV | {"SSC_PUBLIC_TAG_KEY": "1')"}, "SSC_PUBLIC_TAG_KEY"),
        ({k: v for k, v in EXPECTED_ENV.items() if k != "SSC_ORG_ID"}, "missing SSC_ORG_ID"),
        (EXPECTED_ENV | {"SSC_ORG_ID": "organizations/1"}, "SSC_ORG_ID"),
        ({k: v for k, v in EXPECTED_ENV.items() if k != "SSC_OPERATOR"}, "missing SSC_OPERATOR"),
        (EXPECTED_ENV | {"SSC_OPERATOR": "operator@example.com"}, "SSC_OPERATOR"),
        (EXPECTED_ENV | {"SSC_OPERATOR": 'user:a"b@example.com'}, "SSC_OPERATOR"),
    ],
)
def test_the_tag_and_org_must_be_set_and_well_formed(environ: dict[str, str], named: str) -> None:
    with pytest.raises(cloud.CloudReadError, match=named):
        op.load_expected(environ)


async def test_the_run_needs_the_public_tag() -> None:
    environ = {op.PROJECT_ENV: PROJECT, op.FOLDER_ENV: FOLDER}
    with pytest.raises(cloud.CloudReadError, match=op.TAG_KEY_ENV):
        await op.main_async(environ)


async def test_a_policy_gone_from_the_folder_fails() -> None:
    folder = [p for p in folder_policies() if not p["name"].endswith("sql.restrictPublicIp")]
    result, _ = await run(folder)
    assert (result.status, result.reason) == (
        ev.FAIL,
        "sql.restrictPublicIp: not set on the folder",
    )


async def test_a_policy_on_the_folder_that_is_not_expected_fails() -> None:
    extra = {
        "name": f"folders/{FOLDER}/policies/compute.skipDefaultNetworkCreation",
        "spec": {"rules": [{"enforce": True}]},
    }
    result, _ = await run([*folder_policies(), extra])
    assert result.reason == "compute.skipDefaultNetworkCreation: set on the folder and not expected"


async def test_a_policy_set_on_the_cell_project_fails() -> None:
    own = {
        "name": f"projects/{PROJECT}/policies/gcp.resourceLocations",
        "spec": {"rules": [{"allowAll": True}]},
    }
    result, _ = await run(folder_policies(), [own])
    assert result.status == ev.FAIL
    assert result.reason == "gcp.resourceLocations: set on the cell project itself"


async def test_a_refused_read_is_no_read_access() -> None:
    result, text = await run(folder_policies(), refused=True)
    assert result == ev.Result(matrix.ORG_POLICIES, ev.SKIPPED, ev.NO_READ_ACCESS)
    assert text == ""


def test_the_operator_is_the_configured_one_and_other_members_are_kept() -> None:
    expected = EXPECTED["iam.managed.allowedPolicyMembers"]
    assert expected["parameters"]["allowedMemberSubjects"] == [OPERATOR]
    spec = op.normalise(live("iam.managed.allowedPolicyMembers", expected))
    assert spec == expected
    other = {**expected["parameters"], "allowedMemberSubjects": ["user:someone@example.com"]}
    spec = op.normalise(live("iam.managed.allowedPolicyMembers", {**expected, "parameters": other}))
    assert spec != expected
    assert op.normalise({"name": "x/policies/y"})["mode"].startswith("unrecognised")


def test_the_expected_file_names_one_policy_per_cell_constraint() -> None:
    assert len(EXPECTED) == 10
    assert all(
        set(spec) == {"mode", "values", "parameters", "exception", "inherit_from_parent"}
        for spec in EXPECTED.values()
    )
    assert all(spec["values"] == sorted(spec["values"]) for spec in EXPECTED.values())
    assert [c for c, s in EXPECTED.items() if s["exception"] is not None] == [
        "iam.managed.allowedPolicyMembers"
    ]
    assert all(spec["inherit_from_parent"] is False for spec in EXPECTED.values())


async def test_the_run_adds_its_result_to_the_cells_evidence_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSC_ACCESS_TOKEN", "token")
    path = tmp_path / "evidence.json"
    environ = {op.PROJECT_ENV: PROJECT, op.FOLDER_ENV: FOLDER, ev.EVIDENCE_ENV: str(path)}
    environ |= EXPECTED_ENV
    client = httpx2.AsyncClient(transport=transport(copy.deepcopy(folder_policies()), []))
    result, _ = await op.main_async(environ, client=client)
    assert result.status == ev.OK
    assert ev.read_file(path) == ev.Evidence(PROJECT, peer=False, results=(result,))


async def test_the_run_needs_the_project_and_the_folder() -> None:
    with pytest.raises(cloud.CloudReadError, match=op.PROJECT_ENV):
        await op.main_async({})
