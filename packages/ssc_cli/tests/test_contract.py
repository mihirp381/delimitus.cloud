"""The CLI's two contracts: what it reads from the API, and the JSON it prints."""

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from ssc_cli import models
from ssc_cli.commands import access, deploy, lifecycle, logs, releases, rollback, share
from ssc_cli.commands.share import DEFAULT_ROLE, SUBJECT_KINDS, Env, Role
from ssc_cli.shapes import SHAPES

ROOT = Path(__file__).resolve().parents[3]
OPENAPI = ROOT / "docs" / "api" / "openapi.json"
SHAPES_FILE = Path(__file__).resolve().parent / "json_shapes.json"

RESPONSES = (
    models.Whoami,
    models.AppSummary,
    models.AppList,
    models.EnvironmentOut,
    models.AppOut,
    models.GrantOut,
    models.GrantsOut,
    models.GrantsPending,
    models.OperationOut,
    models.OperationAccepted,
    models.UploadTarget,
    models.BundleOut,
    models.CapabilityChange,
    models.CapabilityDiff,
    models.BuildAccepted,
    models.BuildOut,
    models.ActorOut,
    models.ReleaseOut,
    models.ReleaseList,
    models.UserMatch,
    models.UserMatches,
    models.GroupMatch,
    models.GroupMatches,
    models.KillSwitchAccepted,
    models.KillSwitchStep,
    models.KillSwitchRun,
    models.ExplainedGrant,
    models.AccessExplained,
    models.SecretOut,
    models.SecretList,
    models.SecretGrantOut,
    models.SecretSetOut,
    models.DatabaseOut,
    models.LogLineOut,
    models.LogPageOut,
    models.HealthOut,
    models.UsageOut,
    models.PolicyHost,
    models.PolicyConnection,
    models.PolicyApproval,
    models.SubjectDoc,
    models.CeilingDoc,
    models.ConnectionOut,
    models.ConnectionsOut,
    models.EnvironmentConnectionOut,
    models.EnvironmentConnectionsOut,
    models.PolicyDatabase,
    models.DeploymentPolicy,
    models.Approval,
    models.ApprovalPage,
    models.DiffGrant,
    models.GrantDiff,
    models.ApprovalConnection,
    models.ApprovalDetail,
    models.ApprovalDecided,
)
REQUESTS = (
    models.AppCreate,
    models.GrantIn,
    models.GrantsIn,
    models.BundleCreate,
    models.BuildCreate,
    models.DeploymentCreate,
    models.PromoteIn,
    models.KillSwitchCreate,
    models.SecretSet,
    models.PersonDecisionIn,
)


def _type(prop: dict[str, Any], defs: dict[str, Any]) -> tuple[str, bool]:
    """A property's type as ``(name, nullable)``: ``string``, ``array<GrantOut>``, ``GrantOut``."""
    if "anyOf" in prop:
        options = [o for o in prop["anyOf"] if o.get("type") != "null"]
        assert len(options) == 1, prop
        return _type(options[0], defs)[0], len(options) < len(prop["anyOf"])
    if "$ref" in prop:
        name = prop["$ref"].rsplit("/", 1)[-1]
        target = defs[name]
        return (target["type"] if "enum" in target else name), False
    if prop.get("type") == "array":
        return f"array<{_type(prop['items'], defs)[0]}>", False
    return prop["type"], False


def _schema(model: type[BaseModel]) -> tuple[dict[str, Any], set[str], dict[str, Any]]:
    s = model.model_json_schema()
    return s["properties"], set(s.get("required", [])), s.get("$defs", {})


@pytest.fixture(scope="module")
def server() -> dict[str, Any]:
    return json.loads(OPENAPI.read_text())["components"]["schemas"]


@pytest.mark.parametrize("model", RESPONSES + REQUESTS, ids=lambda m: m.__name__)
def test_client_models_match_openapi(server, model):
    props, required, defs = _schema(model)
    sprops, srequired = (
        server[model.__name__]["properties"],
        set(server[model.__name__].get("required", [])),
    )
    assert set(props) <= set(sprops), f"the API has no {set(props) - set(sprops)}"
    for name, prop in props.items():
        mine, mine_null = _type(prop, defs)
        theirs, theirs_null = _type(sprops[name], server)
        assert mine == theirs, f"{model.__name__}.{name}: {mine} != {theirs}"
        if model in RESPONSES:
            assert name in srequired or name not in required, f"{name} may be absent"
            assert mine_null or not theirs_null, f"{model.__name__}.{name} can be null"
        else:
            assert theirs_null or not mine_null, f"{model.__name__}.{name} cannot be null"
    if model in REQUESTS:
        assert srequired <= set(props)


def test_values_the_cli_sends_are_allowed(server):
    grant = server["GrantIn"]["properties"]
    assert {r.value for r in Role} | set(DEFAULT_ROLE.values()) <= set(grant["role"]["enum"])
    kinds = {*SUBJECT_KINDS.values(), "org"}
    assert kinds <= set(grant["subject_kind"]["enum"])
    assert {e.value for e in Env} == set(server["EnvironmentOut"]["properties"]["name"]["enum"])
    assert deploy.PREVIEW in {e.value for e in Env}
    kinds = set(server["DeploymentCreate"]["properties"]["kind"]["enum"])
    assert {deploy.DEPLOY, rollback.ROLLBACK} <= kinds
    commit = server["BundleCreate"]["properties"]["source_commit"]["anyOf"][0]["pattern"]
    assert f"^{deploy.COMMIT.pattern}$" == commit


def test_release_paging_matches_the_api():
    paths = json.loads(OPENAPI.read_text())["paths"]
    params = {
        p["name"]: p["schema"] for p in paths["/v1/apps/{app_id}/releases"]["get"]["parameters"]
    }
    assert params["limit"]["maximum"] == releases.MAX_PAGE
    before = params["before"]["anyOf"][0]
    assert (before["minimum"], before["maximum"]) == (1, releases.MAX_BEFORE)


def test_lookups_are_checked_as_the_api_checks_them():
    paths = json.loads(OPENAPI.read_text())["paths"]
    (email,) = paths["/v1/users"]["get"]["parameters"]
    e = email["schema"]
    assert (email["name"], e["pattern"]) == ("email", f"^{share.EMAIL.pattern}$")
    assert (e["minLength"], e["maxLength"]) == (share.MIN_EMAIL, share.MAX_EMAIL)
    (name,) = paths["/v1/groups"]["get"]["parameters"]
    assert (name["name"], name["schema"]["minLength"]) == ("name", 1)
    assert name["schema"]["maxLength"] == share.MAX_GROUP_NAME


def test_admin_and_access_values_match_the_api(server):
    assert set(lifecycle.MODE_STATUS) == set(
        server["KillSwitchCreate"]["properties"]["mode"]["enum"]
    )
    statuses = set(server["InventoryApp"]["properties"]["status"]["enum"])
    assert set(lifecycle.MODE_STATUS.values()) < statuses
    paths = json.loads(OPENAPI.read_text())["paths"]
    params = paths["/v1/apps/{app_id}/environments/{environment_id}/access"]["get"]["parameters"]
    (user_id,) = [p for p in params if p["name"] == "user_id"]
    assert user_id["schema"]["anyOf"][0]["pattern"] == f"^{access.USER_ID.pattern}$"
    (builder,) = paths["/v1/apps"]["get"]["parameters"]
    assert builder["name"] == "builder"
    assert builder["schema"]["anyOf"][0]["const"] == "me"


def test_log_queries_are_checked_as_the_api_checks_them(server):
    paths = json.loads(OPENAPI.read_text())["paths"]
    get = paths["/v1/apps/{app_id}/environments/{environment_id}/logs"]["get"]
    params = {p["name"]: p["schema"] for p in get["parameters"]}
    assert params["since"]["maximum"] == logs.since_seconds("7d")
    assert params["wait"]["maximum"] >= logs.FOLLOW_WAIT
    assert {s.value for s in logs.Source} == set(server["LogSource"]["enum"])


# ── --json shapes: append-only ───────────────────────────────────────────────


def _shape_fields(model: type[BaseModel]) -> dict[str, str]:
    props, _, defs = _schema(model)
    out: dict[str, str] = {}
    for name, prop in props.items():
        kind, nullable = _type(prop, defs)
        enum = prop.get("enum") or defs.get(prop.get("$ref", "").rsplit("/", 1)[-1], {}).get("enum")
        if enum:
            kind += "[" + ",".join(sorted(enum)) + "]"
        out[name] = kind + ("|null" if nullable else "")
    return out


_ENUM = re.compile(r"^(\w+)\[([^\]]*)\](\|null)?$")


def _widens(old: str, new: str) -> bool:
    """True if ``new`` is ``old`` with more enum values: a JSON value may gain values, not lose."""
    a, b = _ENUM.match(old), _ENUM.match(new)
    if a is None or b is None or (a[1], a[3]) != (b[1], b[3]):
        return False
    return set(a[2].split(",")) < set(b[2].split(","))


def test_json_shapes_are_append_only():
    current = {name: _shape_fields(model) for name, model in SHAPES.items()}
    recorded: dict[str, dict[str, str]] = json.loads(SHAPES_FILE.read_text())
    widened: set[str] = set()
    for shape, fields in recorded.items():
        assert shape in current, f"{shape} was removed"
        for field, kind in fields.items():
            assert field in current[shape], f"{shape}.{field} was removed"
            if current[shape][field] != kind:
                assert _widens(kind, current[shape][field]), f"{shape}.{field} changed type"
                widened.add(f"{shape}.{field}")
    added = {
        f"{shape}.{field}"
        for shape, fields in current.items()
        for field in fields
        if field not in recorded.get(shape, {})
    }
    changed = added | widened
    if changed and os.environ.get("SSC_UPDATE_JSON_SHAPES") == "1":
        SHAPES_FILE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        return
    assert not changed, f"record {sorted(changed)} with SSC_UPDATE_JSON_SHAPES=1 pytest"


def test_enum_values_may_be_added_but_not_removed():
    assert _widens("string[a,b]", "string[a,b,c]")
    assert _widens("string[a]|null", "string[a,b]|null")
    assert not _widens("string[a,b]", "string[a]")
    assert not _widens("string[a,b]", "string[a,c]")
    assert not _widens("string[a]", "string[a,b]|null")
    assert not _widens("string", "string[a]")
