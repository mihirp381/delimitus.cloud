"""CD-5.2: an environment's form targets, admin-edited, audited and compiled into the snapshot."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
import test_deploy
from ssc_testkit import SigningKey, assert_problem, auth, mint, new_key
from test_cell_resources import jobs
from test_deploy import Bench, get, rows_of

from ssc_contracts.errors import ErrorCode
from ssc_contracts.snapshot import MAX_FORM_TARGETS
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.db import bound_org
from ssc_control.snapshot.compiler import compile_document
from ssc_control.snapshot.service import compile_lock

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b

PAY = "https://pay.testorg.com"
SSO = "https://sso.testorg.com"


def put_targets(
    b: Bench, env: str, origins: list[Any], token: str | None = None
) -> httpx2.Response:
    return b.client.put(
        f"/v1/apps/{b.w.app}/environments/{env}/form-targets",
        json={"origins": origins},
        headers=auth(token or b.t.admin, **{IDEMPOTENCY_HEADER: new_key()}),
    )


def audited(b: Bench) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select action, actor_id, target_id, before, after from ssc.audit_event "
        "where target_kind = 'environment' order by seq",
    )


def targets_in_app(b: Bench) -> dict[str, list[str]]:
    envs = get(b, f"/v1/apps/{b.w.app}", b.t.member).json()["environments"]
    return {e["id"]: e["form_targets"] for e in envs}


async def compiled(b: Bench) -> Any:
    async with bound_org(b.ports.engine, b.w.org) as conn:
        return await compile_document(conn, b.w.org, version=1, compiled_at=datetime.now(UTC))


async def test_an_admin_replaces_the_list_and_it_is_audited(b: Bench) -> None:
    assert targets_in_app(b) == {b.w.prod: [], b.w.preview: []}
    r = put_targets(b, b.w.prod, [SSO, PAY, SSO])
    assert r.status_code == 200, r.text
    assert r.json() == {"environment_id": b.w.prod, "origins": [PAY, SSO]}
    assert targets_in_app(b) == {b.w.prod: [PAY, SSO], b.w.preview: []}
    (added,) = audited(b)
    assert (added["action"], added["actor_id"], added["target_id"]) == (
        "org.updated",
        b.w.admin,
        b.w.prod,
    )
    assert (added["before"], added["after"]) == ({"form_targets": []}, {"form_targets": [PAY, SSO]})
    assert len(jobs(b, compile_lock(b.w.org))) == 1

    assert put_targets(b, b.w.prod, [PAY, SSO]).status_code == 200
    assert len(audited(b)) == 1
    assert put_targets(b, b.w.prod, [PAY]).json()["origins"] == [PAY]
    assert put_targets(b, b.w.prod, []).json()["origins"] == []
    assert [(a["before"], a["after"]) for a in audited(b)[1:]] == [
        ({"form_targets": [PAY, SSO]}, {"form_targets": [PAY]}),
        ({"form_targets": [PAY]}, {"form_targets": []}),
    ]
    assert targets_in_app(b)[b.w.prod] == []


async def test_only_an_admin_outside_an_agent_session_sets_the_list(
    b: Bench, signing_key: SigningKey
) -> None:
    assert_problem(put_targets(b, b.w.prod, [PAY], b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(put_targets(b, b.w.prod, [PAY], b.t.builder), ErrorCode.FORBIDDEN)
    session = mint(
        signing_key, org=b.w.org, sub=b.w.admin, jti=f"cred_{new_key()[:16]}", agent=True
    )
    assert_problem(put_targets(b, b.w.prod, [PAY], session), ErrorCode.AGENT_SESSION_REFUSED)
    assert targets_in_app(b)[b.w.prod] == []
    assert audited(b) == []


async def test_an_unknown_environment_is_not_found(b: Bench) -> None:
    assert_problem(put_targets(b, "env_aaaaaaaaaaaaaaaaaaaa", [PAY]), ErrorCode.NOT_FOUND)
    r = b.client.put(
        f"/v1/apps/app_aaaaaaaaaaaaaaaaaaaa/environments/{b.w.prod}/form-targets",
        json={"origins": [PAY]},
        headers=auth(b.t.admin, **{IDEMPOTENCY_HEADER: new_key()}),
    )
    assert_problem(r, ErrorCode.NOT_FOUND)


@pytest.mark.parametrize(
    "origins",
    [
        ["http://pay.testorg.com"],
        ["https://pay.testorg.com/"],
        ["https://pay.testorg.com:8443"],
        ["https://Pay.testorg.com"],
        ["https://localhost"],
        ["pay.testorg.com"],
        [7],
        [f"https://f{i}.testorg.com" for i in range(MAX_FORM_TARGETS + 1)],
    ],
)
async def test_a_bad_list_is_refused(b: Bench, origins: list[Any]) -> None:
    assert_problem(put_targets(b, b.w.prod, origins), ErrorCode.VALIDATION_FAILED)
    assert targets_in_app(b)[b.w.prod] == []


async def test_twenty_targets_are_allowed(b: Bench) -> None:
    origins = sorted(f"https://f{i}.testorg.com" for i in range(MAX_FORM_TARGETS))
    assert put_targets(b, b.w.prod, origins).json()["origins"] == origins


async def test_the_snapshot_carries_each_environment_s_targets(b: Bench) -> None:
    doc = await compiled(b)
    assert doc.form_targets is None
    assert "form_targets" not in doc.model_dump()
    assert put_targets(b, b.w.preview, [SSO, PAY]).status_code == 200
    doc = await compiled(b)
    assert doc.form_targets == {b.w.preview: (PAY, SSO)}
    assert put_targets(b, b.w.prod, [SSO]).status_code == 200
    assert (await compiled(b)).form_targets == {b.w.preview: (PAY, SSO), b.w.prod: (SSO,)}
    assert put_targets(b, b.w.preview, []).status_code == 200
    assert put_targets(b, b.w.prod, []).status_code == 200
    assert (await compiled(b)).form_targets is None
