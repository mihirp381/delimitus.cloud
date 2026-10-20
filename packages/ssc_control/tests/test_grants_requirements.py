"""K-1: a person learns which approvals a sharing change needs, against postgres:18.

``PUT .../grants`` refuses a person's change that needs approval with ``409 APPROVAL_REQUIRED``,
whose wire body is the frozen ``Problem`` and names nothing. ``POST .../grants/requirements``
runs the same check on the same grants and says what to ask for; it writes nothing.
"""

from __future__ import annotations

from typing import Any

import psycopg
import test_approvals
from fastapi.testclient import TestClient
from ssc_testkit import Dsns, assert_problem, auth
from test_approvals import (
    Tokens,
    World,
    approvals_of,
    ask_api,
    current,
    decision,
    grants_path,
    keyed,
    post,
    put,
)
from test_connections import (
    ORG_GRANT,
    add_group,
    ask_exceed,
    ceiling_doc,
    finance,
    link,
)

from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.db import bind_org_sync

client = test_approvals.client
world = test_approvals.world
tokens = test_approvals.tokens


def requirements(
    client: TestClient, w: World, env: str, token: str, grants: list[dict[str, Any]]
) -> Any:
    return post(client, f"{grants_path(w, env)}/requirements", token, {"grants": grants})


def required(
    client: TestClient, w: World, env: str, token: str, grants: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    r = requirements(client, w, env, token, grants)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"environment_id", "grants_version", "requirements"}
    assert body["environment_id"] == env
    assert body["grants_version"] == current(client, w, env, token)["grants_version"]
    return body["requirements"]


def org_wide(client: TestClient, w: World, tokens: Tokens) -> list[dict[str, Any]]:
    return [*keyed(current(client, w, w.prod, tokens.admin)["grants"]), ORG_GRANT]


def data_connected(client: TestClient, w: World, tokens: Tokens) -> None:
    """The app asks for company data in prod, so widening its audience needs approval."""
    assert ask_api(client, tokens.builder, w.prod, subject="finance").status_code == 201


def over_ceiling(client: TestClient, w: World, tokens: Tokens, dsns: Dsns) -> None:
    """Prod uses the ``finance`` connection, whose ceiling is one group of the builder."""
    group = add_group(dsns.app, w, [w.builder])
    finance(client, w, tokens, ceiling_doc(("group", group)))
    assert link(client, w, w.prod, tokens.admin).status_code == 200


WIDEN = {"kind": "widen_audience", "connection": None, "approval_id": None, "state": None}
EXCEED = {"kind": "exceed_ceiling", "connection": "finance", "approval_id": None, "state": None}


def test_widening_a_data_connected_app_requires_widen_audience(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    data_connected(client, world, tokens)
    wide = org_wide(client, world, tokens)
    assert_problem(
        put(client, world, world.prod, tokens.admin, wide, 1), ErrorCode.APPROVAL_REQUIRED
    )
    asked = len(approvals_of(dsns.app, world.org))
    assert required(client, world, world.prod, tokens.admin, wide) == [WIDEN]
    assert len(approvals_of(dsns.app, world.org)) == asked  # nothing opened
    assert current(client, world, world.prod, tokens.admin)["grants_version"] == 1


def test_passing_a_ceiling_requires_exceed_ceiling_with_the_connection(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    over_ceiling(client, world, tokens, dsns)
    wide = org_wide(client, world, tokens)
    assert required(client, world, world.prod, tokens.admin, wide) == [EXCEED]
    assert approvals_of(dsns.app, world.org) == []


def test_widening_past_a_ceiling_requires_both(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    over_ceiling(client, world, tokens, dsns)
    connect = ask_api(client, tokens.builder, world.prod, subject="finance")
    decided = post(
        client,
        f"/v1/approvals/{connect.json()['id']}/decision",
        tokens.operator,
        decision(world.approver),
    )
    assert decided.status_code == 200, decided.text
    wide = org_wide(client, world, tokens)
    assert required(client, world, world.prod, tokens.admin, wide) == [WIDEN, EXCEED]


def test_a_request_already_asked_is_named_with_its_state(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    over_ceiling(client, world, tokens, dsns)
    data_connected(client, world, tokens)
    wide = org_wide(client, world, tokens)
    widen = ask_api(client, tokens.admin, world.prod, "widen_audience", None, {"grants": wide})
    assert widen.status_code == 201, widen.text
    exceed = ask_exceed(client, tokens.admin, world, wide)
    assert required(client, world, world.prod, tokens.admin, wide) == [
        {**WIDEN, "approval_id": widen.json()["id"], "state": "pending"},
        {**EXCEED, "approval_id": exceed, "state": "pending"},
    ]
    # An approved requirement is no longer listed; once all are, the PUT applies.
    for apr in (widen.json()["id"], exceed):
        approver = world.member if apr == exceed else world.approver
        r = post(client, f"/v1/approvals/{apr}/decision", tokens.operator, decision(approver))
        assert r.status_code == 200, r.text
        if apr == widen.json()["id"]:
            assert required(client, world, world.prod, tokens.admin, wide) == [
                {**EXCEED, "approval_id": exceed, "state": "pending"}
            ]
    assert required(client, world, world.prod, tokens.admin, wide) == []
    assert put(client, world, world.prod, tokens.admin, wide, 1).status_code == 200


def test_nothing_is_required_for_a_change_that_needs_no_approval(
    client: TestClient, world: World, tokens: Tokens
) -> None:
    wide = org_wide(client, world, tokens)
    assert required(client, world, world.prod, tokens.admin, wide) == []
    builders = [{"role": "builder", "subject_kind": "org", "subject_id": None}]
    assert required(client, world, world.preview, tokens.admin, builders) == []


def test_an_agent_session_gets_the_same_answer_and_nothing_is_opened(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    data_connected(client, world, tokens)
    wide = org_wide(client, world, tokens)
    asked = len(approvals_of(dsns.app, world.org))
    got = required(client, world, world.prod, tokens.admin_agent, wide)
    assert [r["kind"] for r in got] == ["agent_share", "widen_audience"]
    assert len(approvals_of(dsns.app, world.org)) == asked


def test_who_may_ask_and_what_is_refused(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    wide = org_wide(client, world, tokens)
    assert_problem(
        requirements(client, world, world.prod, tokens.member, wide), ErrorCode.FORBIDDEN
    )
    assert_problem(
        requirements(client, world, new_id("env"), tokens.admin, wide), ErrorCode.NOT_FOUND
    )
    below_floor = [{"role": "user", "subject_kind": "org", "subject_id": None}]
    assert_problem(
        requirements(client, world, world.preview, tokens.admin, below_floor),
        ErrorCode.VALIDATION_FAILED,
    )
    no_key: Any = client.post(
        f"{grants_path(world, world.prod)}/requirements",
        json={"grants": wide},
        headers=auth(tokens.admin),
    )
    assert_problem(no_key, ErrorCode.IDEMPOTENCY_KEY_REQUIRED)
    with_quarantine(dsns, world)
    assert_problem(
        requirements(client, world, world.prod, tokens.admin, wide), ErrorCode.APP_NOT_ACTIVE
    )
    assert approvals_of(dsns.app, world.org) == []


def with_quarantine(dsns: Dsns, w: World) -> None:
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, w.org)
        conn.execute("update ssc.app set status = 'quarantined' where id = %s", (w.app,))
