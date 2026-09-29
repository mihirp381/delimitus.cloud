"""The CLI's two contracts: what it reads from the API, and the JSON it prints."""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from ssc_cli import models
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
    models.OperationOut,
)
REQUESTS = (models.AppCreate, models.GrantIn, models.GrantsIn)


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
        set(server[model.__name__]["required"]),
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


def test_json_shapes_are_append_only():
    current = {name: _shape_fields(model) for name, model in SHAPES.items()}
    recorded: dict[str, dict[str, str]] = json.loads(SHAPES_FILE.read_text())
    for shape, fields in recorded.items():
        assert shape in current, f"{shape} was removed"
        for field, kind in fields.items():
            assert field in current[shape], f"{shape}.{field} was removed"
            assert current[shape][field] == kind, f"{shape}.{field} changed type"
    added = {
        f"{shape}.{field}"
        for shape, fields in current.items()
        for field in fields
        if field not in recorded.get(shape, {})
    }
    if added and os.environ.get("SSC_UPDATE_JSON_SHAPES") == "1":
        merged = {s: {**current[s], **recorded.get(s, {})} for s in current}
        SHAPES_FILE.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
        return
    assert not added, f"record {sorted(added)} with SSC_UPDATE_JSON_SHAPES=1 pytest"
