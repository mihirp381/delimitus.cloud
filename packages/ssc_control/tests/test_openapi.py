"""SSC-011: the committed OpenAPI file is current, and a breaking change is caught."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ssc_contracts.errors import CATALOGUE, PROBLEM_MEDIA_TYPE, ErrorCode
from ssc_control.api.openapi import build_spec, spec_json
from ssc_control.api.routes.common import problem_responses

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "docs" / "api" / "openapi.json"
TOOLS = ROOT / "tools"
FIXTURES = ROOT / "gates" / "fixtures" / "openapi"


def breaking(old: Path, new: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOLS / "openapi_breaking.py"), str(old), str(new)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_committed_spec_matches_the_application() -> None:
    assert SPEC.read_text() == spec_json(), "run: uv run python tools/openapi_check.py --write"


def test_spec_is_deterministic() -> None:
    assert spec_json() == spec_json()


def test_every_refusal_is_documented_as_a_problem() -> None:
    spec = build_spec()
    assert "Problem" in spec["components"]["schemas"]
    for path, item in spec["paths"].items():
        for method, op in item.items():
            for status, response in op["responses"].items():
                if int(status) < 400:
                    continue
                content = response["content"]
                assert list(content) == [PROBLEM_MEDIA_TYPE], (method, path, status)
                assert content[PROBLEM_MEDIA_TYPE]["schema"] == {
                    "$ref": "#/components/schemas/Problem"
                }


def test_every_post_documents_the_idempotency_refusals() -> None:
    spec = build_spec()
    for path, item in spec["paths"].items():
        if "post" not in item:
            continue
        statuses = {int(s) for s in item["post"]["responses"]}
        assert {400, 409, 422}.issubset(statuses), path
        desc = item["post"]["responses"]["400"]["description"]
        assert ErrorCode.IDEMPOTENCY_KEY_REQUIRED.value in desc


def _codes(op: dict[str, Any], status: int) -> set[str]:
    return {c.strip(" `") for c in op["responses"][str(status)]["description"].split(",")}


def test_every_post_lists_each_idempotency_refusal_under_its_status() -> None:
    spec = build_spec()
    shared = (
        ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        ErrorCode.IDEMPOTENCY_KEY_REUSED,
        ErrorCode.IDEMPOTENCY_IN_FLIGHT,
        ErrorCode.VALIDATION_FAILED,
    )
    for path, item in spec["paths"].items():
        if "post" not in item:
            continue
        for code in shared:
            assert code.value in _codes(item["post"], CATALOGUE[code].status), (path, code)


def test_create_app_lists_every_refusal_that_shares_a_status() -> None:
    op = build_spec()["paths"]["/v1/apps"]["post"]
    assert _codes(op, 409) == {"ALREADY_EXISTS", "IDEMPOTENCY_IN_FLIGHT"}
    assert _codes(op, 422) == {"IDEMPOTENCY_KEY_REUSED", "OWNER_NOT_ACTIVE", "VALIDATION_FAILED"}


def test_problem_responses_merges_a_shared_status_and_lists_a_code_once() -> None:
    out = problem_responses(
        ErrorCode.ALREADY_EXISTS, ErrorCode.IDEMPOTENCY_IN_FLIGHT, ErrorCode.ALREADY_EXISTS
    )
    assert list(out) == [409]
    assert out[409]["description"] == "`ALREADY_EXISTS`, `IDEMPOTENCY_IN_FLIGHT`"


def test_problem_component_lists_the_catalogue_codes() -> None:
    spec = build_spec()
    assert set(spec["components"]["schemas"]["ErrorCode"]["enum"]) == {c.value for c in CATALOGUE}


def test_breaking_checker_fires_on_the_planted_fixture() -> None:
    result = breaking(FIXTURES / "old.json", FIXTURES / "new.json")
    assert result.returncode == 1, result.stdout
    assert "operation removed" in result.stdout
    assert "response property removed" in result.stdout
    assert "type changed" in result.stdout
    assert "enum value 'large' removed" in result.stdout


def test_breaking_checker_passes_an_identical_spec() -> None:
    result = breaking(SPEC, SPEC)
    assert result.returncode == 0, result.stdout


def test_breaking_checker_allows_additive_change(tmp_path: Path) -> None:
    spec = build_spec()
    spec["paths"]["/v1/new-thing"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    spec["components"]["schemas"]["AppOut"]["properties"]["colour"] = {"type": "string"}
    new = tmp_path / "new.json"
    new.write_text(json.dumps(spec))
    result = breaking(SPEC, new)
    assert result.returncode == 0, result.stdout


def test_breaking_checker_catches_a_removed_response_field(tmp_path: Path) -> None:
    spec = build_spec()
    del spec["components"]["schemas"]["AppOut"]["properties"]["slug"]
    spec["components"]["schemas"]["AppOut"]["required"].remove("slug")
    new = tmp_path / "new.json"
    new.write_text(json.dumps(spec))
    result = breaking(SPEC, new)
    assert result.returncode == 1
    assert "slug: response property removed" in result.stdout


def test_breaking_checker_catches_a_new_required_request_field(tmp_path: Path) -> None:
    spec = build_spec()
    schema = spec["components"]["schemas"]["AppCreate"]
    schema["properties"]["region"] = {"type": "string"}
    schema["required"].append("region")
    new = tmp_path / "new.json"
    new.write_text(json.dumps(spec))
    result = breaking(SPEC, new)
    assert result.returncode == 1, result.stdout
    assert "region: request property became required" in result.stdout


def test_breaking_checker_catches_a_new_required_parameter(tmp_path: Path) -> None:
    spec = build_spec()
    spec["paths"]["/v1/apps"]["get"].setdefault("parameters", []).append(
        {"name": "page", "in": "query", "required": True, "schema": {"type": "integer"}}
    )
    new = tmp_path / "new.json"
    new.write_text(json.dumps(spec))
    result = breaking(SPEC, new)
    assert result.returncode == 1
    assert "required query parameter 'page' added" in result.stdout


def test_breaking_checker_catches_a_property_removed_from_a_nullable_object(
    tmp_path: Path,
) -> None:
    spec = build_spec()
    detail = spec["components"]["schemas"]["ApprovalDetail"]["properties"]["connection"]
    assert {"type": "null"} in detail["anyOf"]  # the object is reached through a nullable field
    target = spec["components"]["schemas"]["ApprovalConnection"]
    del target["properties"]["ceiling_audience"]
    target["required"] = [r for r in target.get("required", []) if r != "ceiling_audience"]
    new = tmp_path / "new.json"
    new.write_text(json.dumps(spec))
    result = breaking(SPEC, new)
    assert result.returncode == 1, result.stdout
    assert "connection.ceiling_audience: response property removed" in result.stdout


def test_breaking_checker_catches_a_value_removed_from_a_nullable_enum(tmp_path: Path) -> None:
    spec = build_spec()
    field = spec["components"]["schemas"]["ApprovalDetail"]["properties"]["decision_channel"]
    (values,) = [m["enum"] for m in field["anyOf"] if "enum" in m]
    values.remove("cli")
    new = tmp_path / "new.json"
    new.write_text(json.dumps(spec))
    result = breaking(SPEC, new)
    assert result.returncode == 1, result.stdout
    assert "decision_channel: enum value 'cli' removed" in result.stdout


ENABLE = "/v1/cell/resources/{resource}/enable"


def _resource_parameter(spec: dict[str, Any]) -> dict[str, Any]:
    (param,) = [p for p in spec["paths"][ENABLE]["post"]["parameters"] if p["name"] == "resource"]
    assert param["in"] == "path"
    return param


def test_breaking_checker_catches_a_value_removed_from_a_parameter_enum(tmp_path: Path) -> None:
    spec = build_spec()
    _resource_parameter(spec)["schema"]["enum"].remove("egress")
    new = tmp_path / "new.json"
    new.write_text(json.dumps(spec))
    result = breaking(SPEC, new)
    assert result.returncode == 1, result.stdout
    assert f"POST {ENABLE} path parameter resource: enum value 'egress' removed" in result.stdout


def test_breaking_checker_catches_a_parameter_type_change(tmp_path: Path) -> None:
    spec = build_spec()
    _resource_parameter(spec)["schema"] = {"type": "integer", "title": "Resource"}
    new = tmp_path / "new.json"
    new.write_text(json.dumps(spec))
    result = breaking(SPEC, new)
    assert result.returncode == 1, result.stdout
    assert (
        f"POST {ENABLE} path parameter resource: type changed from string to integer"
        in result.stdout
    )


def test_breaking_checker_catches_a_response_property_made_optional(tmp_path: Path) -> None:
    spec = build_spec()
    spec["components"]["schemas"]["AccessExplained"]["required"].remove("user_id")
    new = tmp_path / "new.json"
    new.write_text(json.dumps(spec))
    result = breaking(SPEC, new)
    assert result.returncode == 1, result.stdout
    access = "GET /v1/apps/{app_id}/environments/{environment_id}/access"
    assert f"{access} response 200.user_id: response property no longer required" in result.stdout


def test_breaking_checker_catches_a_new_required_request_body(tmp_path: Path) -> None:
    spec = build_spec()
    complete = "/v1/apps/{app_id}/bundles/{bundle_id}/complete"
    operation = spec["paths"][complete]["post"]
    assert "requestBody" not in operation
    operation["requestBody"] = {
        "required": True,
        "content": {"application/json": {"schema": {"type": "object"}}},
    }
    new = tmp_path / "new.json"
    new.write_text(json.dumps(spec))
    result = breaking(SPEC, new)
    assert result.returncode == 1, result.stdout
    assert f"POST {complete}: required request body added" in result.stdout


def test_breaking_checker_allows_a_new_optional_request_body(tmp_path: Path) -> None:
    spec = build_spec()
    complete = "/v1/apps/{app_id}/bundles/{bundle_id}/complete"
    spec["paths"][complete]["post"]["requestBody"] = {
        "content": {"application/json": {"schema": {"type": "object"}}},
    }
    new = tmp_path / "new.json"
    new.write_text(json.dumps(spec))
    result = breaking(SPEC, new)
    assert result.returncode == 0, result.stdout


def test_the_readme_route_index_lists_every_operation() -> None:
    readme = (ROOT / "docs" / "api" / "README.md").read_text(encoding="utf-8")
    spec = json.loads(SPEC.read_text())
    missing = [
        f"{method.upper()} {path}"
        for path, ops in spec["paths"].items()
        for method in ops
        if f"| `{method.upper()}` | `{path}` |" not in readme
    ]
    assert missing == [], "run: uv run python tools/openapi_check.py --write"


def test_the_route_index_check_fails_on_a_stale_readme(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# x\n\n<!-- route-index -->\n<!-- /route-index -->\n")
    result = subprocess.run(
        [sys.executable, str(TOOLS / "openapi_check.py"), "--readme", str(readme)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "route index" in result.stdout


def test_the_readme_names_every_field_a_new_connection_takes() -> None:
    readme = (ROOT / "docs" / "api" / "README.md").read_text(encoding="utf-8")
    paragraph = readme.split("`POST /v1/connections`", 1)[1].split("\n\n", 1)[0]
    fields = json.loads(SPEC.read_text())["components"]["schemas"]["ConnectionIn"]["properties"]
    assert [f for f in fields if f"`{f}`" not in paragraph] == []
    assert "older form" in paragraph
