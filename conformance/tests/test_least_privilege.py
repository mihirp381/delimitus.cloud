"""The least-privilege checks against a scripted cell: one planted fault per failure."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from ssc_conformance import cloud_read as cloud
from ssc_conformance import evidence as ev
from ssc_conformance import least_privilege as lp
from ssc_conformance import matrix

PROJECT = "ssc-c-test"
FOLDER = "111"
BUCKET = f"{PROJECT}-cell"
NOW = datetime(2026, 10, 6, 3, 0, tzinfo=UTC)
EXPECTED = json.loads(lp.EXPECTED_DENY.read_text(encoding="utf-8"))
PROOFS = (matrix.BUILD_BUNDLE, matrix.STAFF, matrix.DENY_READ, matrix.VERSIONS_ONLY)
SECRET_READ = EXPECTED["permission"]
BUILD = f"serviceAccount:ssc-build@{PROJECT}.iam.gserviceaccount.com"
CONTROL = "serviceAccount:ssc-control@ssc-control-staging.iam.gserviceaccount.com"

type Json = dict[str, Any]


def principal(account: str, project: str) -> str:
    return f"{lp.PRINCIPAL}{account}@{project}.iam.gserviceaccount.com"


def deny_policy(*rules: Json) -> Json:
    return {"name": f"policies/x/denypolicies/{lp.DENY_POLICY}", "rules": list(rules)}


def rule(principals: list[str], **more: Any) -> Json:
    return {
        "denyRule": {"deniedPrincipals": principals, "deniedPermissions": [SECRET_READ], **more}
    }


def world() -> dict[str, Json]:
    """The cell as configured: what each read returns."""
    ours = [principal(a, PROJECT) for a in EXPECTED["cell"]]
    tagged = {"expression": "!resource.matchTagId('tagKeys/1', 'tagValues/2')"}
    control = [principal(a, "ssc-control-staging") for a in EXPECTED["folder"]]
    return {
        "project": {
            "bindings": [
                {"role": "roles/logging.logWriter", "members": [BUILD]},
                {
                    "role": "roles/run.admin",
                    "members": [f"serviceAccount:ssc-cell-agent@{PROJECT}"],
                },
            ]
        },
        "folder": {
            "bindings": [
                {"role": "roles/iam.securityReviewer", "members": ["serviceAccount:ssc-nightly@x"]}
            ]
        },
        "bucket": {
            "bindings": [{"role": "roles/storage.objectAdmin", "members": ["serviceAccount:a@b"]}]
        },
        "cell-deny": {
            "policies": [
                deny_policy(
                    rule(ours),
                    rule([principal(EXPECTED["cell_data"], PROJECT)], denialCondition=tagged),
                )
            ]
        },
        "folder-deny": {"policies": [deny_policy(rule(control))]},
    }


def transport(
    state: dict[str, Json], *, refused: tuple[str, ...] = (), seen: list[str] | None = None
) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(f"{request.method} {url}")
        for key, found in (
            ("project", f"projects/{PROJECT}:getIamPolicy"),
            ("folder", f"folders/{FOLDER}:getIamPolicy"),
            ("bucket", f"/b/{BUCKET}/iam"),
            ("cell-deny", "cloudresourcemanager.googleapis.com%252Fprojects%252F"),
            ("folder-deny", "cloudresourcemanager.googleapis.com%252Ffolders%252F"),
        ):
            if found in url:
                if key in refused:
                    return httpx2.Response(403, json={"error": {"code": 403}})
                return httpx2.Response(200, json=state[key])
        return httpx2.Response(404)

    return httpx2.MockTransport(handle)


async def run(
    state: dict[str, Json], *, refused: tuple[str, ...] = (), now: datetime = NOW
) -> dict[str, ev.Result]:
    async def token() -> str:
        return "token"

    client = httpx2.AsyncClient(transport=transport(state, refused=refused))
    reader = cloud.CloudReader(token, client=client)
    results = await lp.check(reader, PROJECT, FOLDER, now=now)
    await reader.aclose()
    assert [r.proof for r in results] == list(PROOFS)
    return {r.proof: r for r in results}


def jit(expires: datetime, role: str = lp.JIT_ROLE, member: str = "user:staff@example.com") -> Json:
    stamp = f"{expires:%Y-%m-%dT%H:%M:%S}Z"
    condition = {"title": "PAM", "expression": f'request.time < timestamp("{stamp}")'}
    return {"role": role, "members": [member], "condition": condition}


async def test_a_cell_as_built_passes_all_four() -> None:
    results = await run(world())
    assert {p: r.status for p, r in results.items()} == dict.fromkeys(PROOFS, ev.OK)


async def test_the_deny_policies_are_read_with_the_attachment_encoded_twice() -> None:
    seen: list[str] = []

    async def token() -> str:
        return "token"

    reader = cloud.CloudReader(
        token, client=httpx2.AsyncClient(transport=transport(world(), seen=seen))
    )
    await lp.check(reader, PROJECT, FOLDER, now=NOW)
    await reader.aclose()
    wanted = (
        "GET https://iam.googleapis.com/v2/policies/"
        f"cloudresourcemanager.googleapis.com%252Fprojects%252F{PROJECT}/denypolicies"
    )
    assert wanted in seen
    assert any(s.startswith("POST") and s.endswith(f"folders/{FOLDER}:getIamPolicy") for s in seen)


@pytest.mark.parametrize(
    ("where", "binding", "named"),
    [
        ("project", {"role": "roles/storage.objectViewer", "members": [BUILD]}, "objectViewer"),
        ("bucket", {"role": "roles/storage.objectViewer", "members": [BUILD]}, "cell bucket"),
        ("folder", {"role": "roles/logging.logWriter", "members": [BUILD]}, "the folder"),
    ],
)
async def test_the_build_account_with_a_wider_grant_fails(
    where: str, binding: Json, named: str
) -> None:
    state = world()
    state[where]["bindings"].append(binding)
    result = (await run(state))[matrix.BUILD_BUNDLE]
    assert result.status == ev.FAIL
    assert named in result.reason


@pytest.mark.parametrize(
    "binding",
    [
        {"role": "roles/owner", "members": ["user:staff@example.com"]},
        {"role": "roles/viewer", "members": ["group:everyone@example.com"]},
        {"role": "roles/viewer", "members": ["domain:example.com"]},
        {"role": "roles/viewer", "members": ["allAuthenticatedUsers"]},
        jit(NOW + timedelta(hours=5)),
        jit(NOW + timedelta(minutes=30), role="roles/owner"),
        {"role": lp.JIT_ROLE, "members": ["user:staff@example.com"]},
    ],
)
async def test_standing_access_for_a_person_fails(binding: Json) -> None:
    for where in ("project", "folder"):
        state = world()
        state[where]["bindings"].append(binding)
        result = (await run(state))[matrix.STAFF]
        assert result.status == ev.FAIL
        assert where in result.reason


@pytest.mark.parametrize(
    "binding",
    [
        jit(NOW + timedelta(minutes=45)),
        jit(NOW + timedelta(hours=1)),
        jit(NOW - timedelta(minutes=1), role="roles/owner"),
        {"role": "roles/owner", "members": ["deleted:user:gone@example.com?uid=1"]},
        {
            "role": "roles/owner",
            "members": ["serviceAccount:ssc-deployer@x.iam.gserviceaccount.com"],
        },
    ],
)
async def test_a_just_in_time_grant_an_expired_one_and_service_accounts_are_not_standing(
    binding: Json,
) -> None:
    state = world()
    state["folder"]["bindings"].append(binding)
    assert (await run(state))[matrix.STAFF].status == ev.OK


async def test_staff_access_is_judged_at_the_time_given() -> None:
    state = world()
    state["folder"]["bindings"].append(jit(NOW + timedelta(minutes=30)))
    assert (await run(state, now=NOW + timedelta(hours=2)))[matrix.STAFF].status == ev.OK
    assert (await run(state, now=NOW - timedelta(hours=1)))[matrix.STAFF].status == ev.FAIL


def _without(rules: list[Json], account: str) -> list[Json]:
    out = copy.deepcopy(rules)
    first = out[0]["denyRule"]
    first["deniedPrincipals"] = [p for p in first["deniedPrincipals"] if f"/{account}@" not in p]
    return out


async def test_a_cell_identity_missing_from_the_deny_rule_fails() -> None:
    state = world()
    policy = state["cell-deny"]["policies"][0]
    policy["rules"] = _without(policy["rules"], "ssc-build")
    result = (await run(state))[matrix.DENY_READ]
    assert result.status == ev.FAIL
    assert "ssc-build" in result.reason


async def test_the_deny_rule_with_an_exception_or_a_condition_does_not_count() -> None:
    for more in (
        {"exceptionPrincipals": ["principal://x"]},
        {"denialCondition": {"expression": "x"}},
    ):
        state = world()
        state["cell-deny"]["policies"][0]["rules"][0]["denyRule"].update(more)
        result = (await run(state))[matrix.DENY_READ]
        assert result.status == ev.FAIL
        assert "ssc-gateway" in result.reason


async def test_ssc_data_must_be_refused_untagged_secrets() -> None:
    state = world()
    state["cell-deny"]["policies"][0]["rules"][1]["denyRule"].pop("denialCondition")
    assert (await run(state))[matrix.DENY_READ].status == ev.OK  # refused everything: stricter
    state["cell-deny"]["policies"][0]["rules"][1]["denyRule"]["denialCondition"] = {
        "expression": "resource.name.startsWith('x')"
    }
    result = (await run(state))[matrix.DENY_READ]
    assert result.status == ev.FAIL
    assert "ssc-data" in result.reason


async def test_a_deny_policy_may_name_more_accounts_than_expected() -> None:
    state = world()
    state["cell-deny"]["policies"][0]["rules"][0]["denyRule"]["deniedPrincipals"].append(
        principal("ssc-extra", PROJECT)
    )
    assert (await run(state))[matrix.DENY_READ].status == ev.OK


async def test_a_missing_cell_or_folder_deny_policy_fails() -> None:
    state = world()
    state["cell-deny"] = {}
    result = (await run(state))[matrix.DENY_READ]
    assert result.status == ev.FAIL
    assert "the cell has no" in result.reason
    state = world()
    state["folder-deny"] = {"policies": []}
    result = (await run(state))[matrix.DENY_READ]
    assert "the folder has no" in result.reason
    assert (await run(state))[matrix.VERSIONS_ONLY].status == ev.FAIL


async def test_a_control_account_missing_from_the_folder_deny_fails_both_checks() -> None:
    state = world()
    state["folder-deny"]["policies"][0]["rules"] = _without(
        state["folder-deny"]["policies"][0]["rules"], "ssc-auth"
    )
    results = await run(state)
    for proof in (matrix.DENY_READ, matrix.VERSIONS_ONLY):
        assert results[proof].status == ev.FAIL
        assert "ssc-auth" in results[proof].reason


@pytest.mark.parametrize("where", ["project", "folder"])
@pytest.mark.parametrize(
    "role", ["roles/secretmanager.secretAccessor", "roles/secretmanager.admin", "roles/editor"]
)
async def test_a_control_account_that_reads_secret_values_fails(where: str, role: str) -> None:
    state = world()
    state[where]["bindings"].append({"role": role, "members": [CONTROL]})
    result = (await run(state))[matrix.VERSIONS_ONLY]
    assert result.status == ev.FAIL
    assert role in result.reason


async def test_a_control_account_with_a_custom_role_fails_and_one_with_versions_adder_passes() -> (
    None
):
    state = world()
    state["project"]["bindings"].append(
        {"role": "roles/secretmanager.secretVersionAdder", "members": [CONTROL]}
    )
    assert (await run(state))[matrix.VERSIONS_ONLY].status == ev.OK
    state["project"]["bindings"].append({"role": "projects/p/roles/custom", "members": [CONTROL]})
    result = (await run(state))[matrix.VERSIONS_ONLY]
    assert result.status == ev.FAIL
    assert "custom role" in result.reason


async def test_a_refused_read_is_no_read_access_for_the_checks_that_need_it_only() -> None:
    results = await run(world(), refused=("folder",))
    assert results[matrix.BUILD_BUNDLE] == ev.Result(
        matrix.BUILD_BUNDLE, ev.SKIPPED, ev.NO_READ_ACCESS
    )
    assert results[matrix.STAFF].reason == ev.NO_READ_ACCESS
    assert results[matrix.VERSIONS_ONLY].reason == ev.NO_READ_ACCESS
    assert results[matrix.DENY_READ].status == ev.OK
    results = await run(world(), refused=("folder-deny", "cell-deny"))
    assert results[matrix.DENY_READ].status == ev.SKIPPED
    assert results[matrix.VERSIONS_ONLY].status == ev.SKIPPED
    assert results[matrix.STAFF].status == ev.OK


async def test_another_failure_to_read_is_an_error_not_a_skip() -> None:
    async def token() -> str:
        return "token"

    broken = httpx2.MockTransport(lambda _: httpx2.Response(500))
    reader = cloud.CloudReader(token, client=httpx2.AsyncClient(transport=broken))
    with pytest.raises(cloud.CloudReadError, match="HTTP 500"):
        await lp.check(reader, PROJECT, FOLDER, now=NOW)
    await reader.aclose()


async def test_the_run_adds_its_four_results_to_the_cells_evidence_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSC_ACCESS_TOKEN", "token")
    path = tmp_path / "evidence.json"
    ev.write(path, ev.Evidence(PROJECT, peer=True, results=(ev.Result("non_root_10001", ev.OK),)))
    environ = {
        lp.PROJECT_ENV: PROJECT,
        lp.FOLDER_ENV: FOLDER,
        ev.EVIDENCE_ENV: str(path),
    }
    results = await lp.main_async(environ, client=httpx2.AsyncClient(transport=transport(world())))
    assert all(r.status == ev.OK for r in results)
    saved = ev.read_file(path)
    assert saved.peer is True
    assert [r.proof for r in saved.results] == ["non_root_10001", *PROOFS]


async def test_the_run_needs_the_project_and_the_folder() -> None:
    with pytest.raises(cloud.CloudReadError, match=lp.FOLDER_ENV):
        await lp.main_async({lp.PROJECT_ENV: PROJECT})


def test_the_expected_deny_file_holds_what_the_checks_read() -> None:
    assert sorted(EXPECTED) == ["cell", "cell_data", "folder", "permission"]
    assert EXPECTED["permission"] == "secretmanager.googleapis.com/versions.access"  # noqa: S105
    assert EXPECTED["cell_data"] not in EXPECTED["cell"]
