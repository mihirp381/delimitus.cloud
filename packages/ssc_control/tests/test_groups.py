"""``GET /v1/groups``: whole-name and prefix matches, on test_access's bench."""

from __future__ import annotations

from typing import Any

import test_access
from fastapi.testclient import TestClient
from ssc_testkit import assert_problem, auth
from test_access import World, set_members, sync_group

from ssc_contracts.errors import ErrorCode

client = test_access.client
world = test_access.world


def find(c: TestClient, w: World, name: str, match: str | None = None) -> Any:
    params = {"name": name} if match is None else {"name": name, "match": match}
    return c.get("/v1/groups", params=params, headers=auth(w.admin_token))


def group(c: TestClient, w: World, ref: str, name: str) -> str:
    r = sync_group(c, w, ref, name)
    assert r.status_code == 200, r.text
    gid: str = r.json()["group_id"]
    assert set_members(c, w, gid, []).status_code == 200
    return gid


def names(r: Any) -> list[str]:
    assert r.status_code == 200, r.text
    return [g["name"] for g in r.json()["groups"]]


def test_a_prefix_match_ignores_case_and_escapes_wildcards(
    client: TestClient, world: World
) -> None:
    for i, name in enumerate(
        ["Finance", "FINANCE ops", "Fin%ops", "Fin_ops", "Fin\\ops", "Finxops", "Sales"]
    ):
        group(client, world, f"okta-00g{i}", name)
    assert sorted(names(find(client, world, "fin", "prefix"))) == [
        "FINANCE ops",
        "Fin%ops",
        "Fin\\ops",
        "Fin_ops",
        "Finance",
        "Finxops",
    ]
    assert names(find(client, world, "Fin%", "prefix")) == ["Fin%ops"]
    assert names(find(client, world, "fin_", "prefix")) == ["Fin_ops"]
    assert names(find(client, world, "fin\\", "prefix")) == ["Fin\\ops"]
    assert names(find(client, world, "finance")) == ["Finance"]
    assert names(find(client, world, "finance", "whole")) == ["Finance"]
    assert_problem(find(client, world, "fin", "contains"), ErrorCode.VALIDATION_FAILED)


def test_a_prefix_match_answers_at_most_twenty(client: TestClient, world: World) -> None:
    for i in range(21):
        group(client, world, f"okta-bulk{i:02d}", f"Bulk {i:02d}")
    found = names(find(client, world, "bulk", "prefix"))
    assert found == [f"Bulk {i:02d}" for i in range(20)]
    assert len(names(find(client, world, "Bulk 20"))) == 1
