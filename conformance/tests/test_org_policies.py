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
OPERATOR = "user:someone@example.com"
EXPECTED = json.loads(op.EXPECTED.read_text(encoding="utf-8"))
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
            subjects = [
                OPERATOR if v == "<operator>" else v
                for v in spec["parameters"]["allowedMemberSubjects"]
            ]
            main["parameters"] = {**spec["parameters"], "allowedMemberSubjects": subjects}
        rules = [main]
    if spec["tag_exception"]:
        rules.append({"enforce": False, "condition": {"expression": TAG}})
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
        return await op.check(reader, PROJECT, FOLDER)
    finally:
        await reader.aclose()


async def test_the_folder_as_declared_passes_and_is_listed() -> None:
    result, text = await run(folder_policies())
    assert result == ev.Result(matrix.ORG_POLICIES, ev.OK, "")
    assert text.count("\n") == len(EXPECTED) + 2
    assert "| gcp.resourceLocations | allow global, in:us-central1-locations |" in text
    assert "| compute.restrictSharedVpcHostProjects | deny all |" in text
    assert "allowedMemberSubjects <operator>" in text
    assert "off where the public-invoker tag is" in text


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
            "allowedMemberSubjects <operator>, <operator>",
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


def test_the_operator_is_any_user_and_other_members_are_kept() -> None:
    spec = op.normalise(
        live("iam.managed.allowedPolicyMembers", EXPECTED["iam.managed.allowedPolicyMembers"])
    )
    assert spec == EXPECTED["iam.managed.allowedPolicyMembers"]
    assert op.normalise({"name": "x/policies/y"})["mode"].startswith("unrecognised")


def test_the_expected_file_names_one_policy_per_cell_constraint() -> None:
    assert len(EXPECTED) == 10
    assert all(
        set(spec) == {"mode", "values", "parameters", "tag_exception"} for spec in EXPECTED.values()
    )
    assert all(spec["values"] == sorted(spec["values"]) for spec in EXPECTED.values())
    assert [c for c, s in EXPECTED.items() if s["tag_exception"]] == [
        "iam.managed.allowedPolicyMembers"
    ]


async def test_the_run_adds_its_result_to_the_cells_evidence_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSC_ACCESS_TOKEN", "token")
    path = tmp_path / "evidence.json"
    environ = {op.PROJECT_ENV: PROJECT, op.FOLDER_ENV: FOLDER, ev.EVIDENCE_ENV: str(path)}
    client = httpx2.AsyncClient(transport=transport(copy.deepcopy(folder_policies()), []))
    result, _ = await op.main_async(environ, client=client)
    assert result.status == ev.OK
    assert ev.read_file(path) == ev.Evidence(PROJECT, peer=False, results=(result,))


async def test_the_run_needs_the_project_and_the_folder() -> None:
    with pytest.raises(cloud.CloudReadError, match=op.PROJECT_ENV):
        await op.main_async({})
