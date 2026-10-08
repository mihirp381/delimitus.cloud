"""SSC-052: connections, classification and the audience ceiling, against postgres:18.

Ticket "done when" checks:
  * sharing a Finance-connected app with the whole org is blocked until approved
        -> test_sharing_a_finance_app_with_the_org_waits_for_its_owner
  * lowering a ceiling flags every app now over it
        -> test_lowering_a_ceiling_flags_every_environment_now_over_it
Plus: the ceiling rules, a user's group membership at the moment of the check, the two approvals
a change can need, grant creation over the ceiling, who may decide, the snapshot, the routes and
who sees which connection.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
import test_approvals
from fastapi.testclient import TestClient
from ssc_testkit import Dsns, SigningKey, assert_problem, auth, mint, new_key
from test_approvals import (
    Tokens,
    World,
    approvals_of,
    ask_api,
    current,
    decision,
    events_of,
    in_org,
    keyed,
    make_world,
    post,
    put,
)

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_contracts.snapshot import SnapshotDoc
from ssc_control.db import bind_org_sync
from ssc_control.domain.approval_rules import check_decider
from ssc_control.domain.audience import (
    ORG,
    Ceiling,
    CeilingError,
    ceiling_json,
    exceeds,
    lowers,
    parse_ceiling,
    with_members,
)
from ssc_control.snapshot.compiler import compile_document

client = test_approvals.client
world = test_approvals.world
tokens = test_approvals.tokens

GROUP = "grp_" + "a" * 20
OTHER_GROUP = "grp_" + "b" * 20
USER = "usr_" + "c" * 20
ORG_GRANT = {"role": "user", "subject_kind": "org", "subject_id": None}
BUILDER_ORG = {"role": "builder", "subject_kind": "org", "subject_id": None}


def listed(*subjects: tuple[str, str]) -> Ceiling:
    return Ceiling(frozenset(subjects))


def ceiling_doc(*subjects: tuple[str, str]) -> dict[str, Any]:
    if not subjects:
        return {"audience": "org"}
    return {
        "audience": "subjects",
        "subjects": [{"kind": k, "id": i} for k, i in subjects],
    }


def user_grant(user: str) -> dict[str, Any]:
    return {"role": "user", "subject_kind": "user", "subject_id": user}


def group_grant(group: str) -> dict[str, Any]:
    return {"role": "user", "subject_kind": "group", "subject_id": group}


def add_group(dsn: str, w: World, members: list[str]) -> str:
    group = new_id("grp")
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, w.org)
        conn.execute(
            "insert into ssc.user_group (id, org_id, directory_ref, display_name) "
            "values (%s, %s, %s, 'Finance')",
            (group, w.org, f"ref-{group}"),
        )
        for user in members:
            conn.execute(
                "insert into ssc.group_member (org_id, group_id, user_id) values (%s, %s, %s)",
                (w.org, group, user),
            )
    return group


def leave_group(dsn: str, w: World, group: str, user: str) -> None:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, w.org)
        conn.execute(
            "delete from ssc.group_member where group_id = %s and user_id = %s", (group, user)
        )


def rows(dsn: str, w: World, statement: str, params: tuple[object, ...] = ()) -> list[Any]:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, w.org)
        return conn.execute(statement, params).fetchall()


def connection_body(name: str = "finance", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "classification": "internal",
        "host": "db.corp.internal",
        "port": 5432,
        "database": "warehouse",
        **extra,
    }


def create_connection(
    client: TestClient, token: str, owner: str, name: str = "finance", **extra: Any
) -> Any:
    return post(
        client, "/v1/connections", token, connection_body(name, owner_user_id=owner, **extra)
    )


def patch(client: TestClient, token: str, name: str, body: dict[str, Any]) -> Any:
    return client.patch(f"/v1/connections/{name}", json=body, headers=auth(token))


def link_path(w: World, env: str, name: str | None = None) -> str:
    base = f"/v1/apps/{w.app}/environments/{env}/connections"
    return base if name is None else f"{base}/{name}"


def link(client: TestClient, w: World, env: str, token: str, name: str = "finance") -> Any:
    return client.put(link_path(w, env, name), json={}, headers=auth(token))


def links_of(client: TestClient, w: World, env: str, token: str) -> list[dict[str, Any]]:
    r = client.get(link_path(w, env), headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()["connections"]


def ready(client: TestClient, tokens: Tokens, name: str = "finance") -> None:
    assert patch(client, tokens.admin, name, {"setup_status": "ready"}).status_code == 200


def decide_as(
    client: TestClient, tokens: Tokens, approval_id: str, approver: str, token: str | None = None
) -> Any:
    return post(
        client,
        f"/v1/approvals/{approval_id}/decision",
        token or tokens.operator,
        decision(approver),
    )


def ask_exceed(
    client: TestClient,
    token: str,
    w: World,
    grants: list[dict[str, Any]],
    name: str = "finance",
    env: str | None = None,
) -> str:
    r = ask_api(
        client,
        token,
        env or w.prod,
        "exceed_ceiling",
        None,
        {"connection": name, "grants": grants},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def finance(
    client: TestClient,
    w: World,
    tokens: Tokens,
    ceiling: dict[str, Any] | None,
    *,
    classification: str = "confidential",
    owner: str | None = None,
) -> None:
    extra: dict[str, Any] = {"classification": classification}
    if ceiling is not None:
        extra["ceiling"] = ceiling
    r = create_connection(client, tokens.admin, owner or w.member, **extra)
    assert r.status_code == 201, r.text


def prod_grants(client: TestClient, w: World, tokens: Tokens) -> tuple[list[dict[str, Any]], int]:
    now = current(client, w, w.prod, tokens.admin)
    return keyed(now["grants"]), now["grants_version"]


def test_a_ceiling_is_the_whole_org_or_a_listed_set() -> None:
    assert parse_ceiling({"audience": "org"}) == ORG
    parsed = parse_ceiling(ceiling_doc(("group", GROUP), ("user", USER)))
    assert parsed == listed(("group", GROUP), ("user", USER))
    assert parse_ceiling(ceiling_json(parsed)) == parsed
    for bad in (
        {},
        {"audience": "org", "subjects": []},
        {"audience": "subjects"},
        {"audience": "subjects", "subjects": []},
        {"audience": "subjects", "subjects": [{"kind": "org", "id": GROUP}]},
        {"audience": "subjects", "subjects": [{"kind": "group", "id": USER}]},
        {"audience": "subjects", "subjects": [{"kind": "user", "id": USER, "x": 1}]},
        {"audience": "everyone"},
    ):
        with pytest.raises(CeilingError):
            parse_ceiling(bad)


def test_who_is_inside_a_ceiling() -> None:
    org_wide = {("user", "org", None)}
    ours = ("user", "user", USER)
    by_group = ("user", "group", GROUP)
    assert not exceeds(ORG, {*org_wide, ours, by_group})
    group_only = listed(("group", GROUP))
    assert not exceeds(group_only, {by_group})
    assert exceeds(group_only, {ours})
    assert exceeds(group_only, {("user", "group", OTHER_GROUP)})
    assert exceeds(group_only, org_wide)
    assert exceeds(listed(("user", USER)), org_wide)
    assert not exceeds(with_members(group_only, [USER]), {ours, by_group})
    assert exceeds(with_members(group_only, []), {ours})
    assert with_members(ORG, [USER]) == ORG


def test_a_ceiling_is_lowered_by_narrowing_or_dropping_a_subject() -> None:
    both = listed(("group", GROUP), ("group", OTHER_GROUP))
    one = listed(("group", GROUP))
    assert lowers(ORG, one)
    assert lowers(both, one)
    assert not lowers(one, both)
    assert not lowers(one, one)
    assert not lowers(one, ORG)
    assert not lowers(ORG, ORG)
    assert lowers(one, listed(("user", USER)))


def test_who_may_decide_an_exceed_request() -> None:
    owner, requester, other = USER, "usr_" + "d" * 20, "usr_" + "e" * 20
    assert check_decider(requester, owner, "member", True, False, connection_owner_id=owner) is None
    assert check_decider(requester, other, "admin", True, False, connection_owner_id=owner) is None
    assert (
        check_decider(requester, other, "member", True, False, connection_owner_id=owner)
        == "not_eligible"
    )
    assert (
        check_decider(requester, owner, "member", False, False, connection_owner_id=owner)
        == "not_eligible"
    )
    assert (
        check_decider(owner, owner, "member", True, False, connection_owner_id=owner)
        == "self_approval"
    )
    assert (
        check_decider(requester, owner, "member", True, True, connection_owner_id=owner)
        == "agent_session"
    )
    assert check_decider(requester, owner, "member", True, False) == "not_eligible"


def test_sharing_a_finance_app_with_the_org_waits_for_its_owner(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    group = add_group(dsns.app, world, [world.builder])
    finance(client, world, tokens, ceiling_doc(("group", group)))
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    before, version = prod_grants(client, world, tokens)
    org_wide = [*before, ORG_GRANT]
    assert_problem(
        put(client, world, world.prod, tokens.admin, org_wide, version), ErrorCode.APPROVAL_REQUIRED
    )
    assert current(client, world, world.prod, tokens.admin)["grants_version"] == version
    apr = ask_exceed(client, tokens.admin, world, org_wide)
    assert_problem(
        put(client, world, world.prod, tokens.admin, org_wide, version), ErrorCode.APPROVAL_REQUIRED
    )
    decided = decide_as(client, tokens, apr, world.member)
    assert decided.status_code == 200, decided.text
    applied = put(client, world, world.prod, tokens.admin, org_wide, version)
    assert applied.status_code == 200, applied.text
    assert applied.json()["grants_version"] == version + 1
    added = [
        e
        for e in events_of(dsns.app, world.org, AuditAction.GRANT_ADDED)
        if e["after"]["environment_id"] == world.prod
    ]
    assert [e["policy_decision_id"] for e in added] == [decided.json()["policy_decision_id"]]
    assert links_of(client, world, world.prod, tokens.admin)[0]["over_ceiling_since"] is None
    narrow = put(client, world, world.prod, tokens.admin, before, version + 1)
    assert narrow.status_code == 200, narrow.text


def test_a_change_inside_the_ceiling_and_an_app_without_connections_wait_for_nobody(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    group = add_group(dsns.app, world, [world.builder, world.member])
    finance(client, world, tokens, ceiling_doc(("group", group)))
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    before, version = prod_grants(client, world, tokens)
    inside = put(
        client, world, world.prod, tokens.admin, [*before, user_grant(world.member)], version
    )
    assert inside.status_code == 200, inside.text
    by_group = put(
        client,
        world,
        world.prod,
        tokens.admin,
        [*before, user_grant(world.member), group_grant(group)],
        version + 1,
    )
    assert by_group.status_code == 200, by_group.text
    assert approvals_of(dsns.app, world.org) == []
    free = put(client, world, world.preview, tokens.admin, [BUILDER_ORG], 1)
    assert free.status_code == 200, free.text


def test_a_user_counts_while_they_are_in_a_listed_group(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    group = add_group(dsns.app, world, [world.builder, world.member])
    finance(client, world, tokens, ceiling_doc(("group", group)))
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    before, version = prod_grants(client, world, tokens)
    outsider = [*before, user_grant(world.approver)]
    assert_problem(
        put(client, world, world.prod, tokens.admin, outsider, version), ErrorCode.APPROVAL_REQUIRED
    )
    other = add_group(dsns.app, world, [])
    assert_problem(
        put(
            client,
            world,
            world.prod,
            tokens.admin,
            [*before, group_grant(other)],
            version,
        ),
        ErrorCode.APPROVAL_REQUIRED,
    )
    member = [*before, user_grant(world.member)]
    assert put(client, world, world.prod, tokens.admin, member, version).status_code == 200
    assert put(client, world, world.prod, tokens.admin, before, version + 1).status_code == 200
    leave_group(dsns.app, world, group, world.member)
    assert_problem(
        put(client, world, world.prod, tokens.admin, member, version + 2),
        ErrorCode.APPROVAL_REQUIRED,
    )


def test_a_group_grant_counts_only_when_that_group_is_listed(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    listed_group = add_group(dsns.app, world, [world.builder])
    other = add_group(dsns.app, world, [world.builder])
    finance(client, world, tokens, ceiling_doc(("group", listed_group)))
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    before, version = prod_grants(client, world, tokens)
    unlisted = [*before, group_grant(other)]
    assert_problem(
        put(client, world, world.prod, tokens.admin, unlisted, version), ErrorCode.APPROVAL_REQUIRED
    )
    listed_ok = [*before, group_grant(listed_group)]
    assert put(client, world, world.prod, tokens.admin, listed_ok, version).status_code == 200


def test_an_org_grant_is_inside_only_an_org_ceiling(
    client: TestClient, world: World, tokens: Tokens
) -> None:
    finance(client, world, tokens, None, classification="internal")
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    before, version = prod_grants(client, world, tokens)
    wide = put(client, world, world.prod, tokens.admin, [*before, ORG_GRANT], version)
    assert wide.status_code == 200, wide.text


def test_a_change_that_widens_and_exceeds_needs_both_approvals(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    group = add_group(dsns.app, world, [world.builder])
    finance(client, world, tokens, ceiling_doc(("group", group)))
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    connect = ask_api(client, tokens.builder, world.prod, subject="finance")
    assert connect.status_code == 201, connect.text
    assert decide_as(client, tokens, connect.json()["id"], world.approver).status_code == 200
    assert rows(dsns.app, world, "select count(*) from ssc.connection_grant")[0][0] == 1
    before, version = prod_grants(client, world, tokens)
    org_wide = [*before, ORG_GRANT]
    assert_problem(
        put(client, world, world.prod, tokens.admin, org_wide, version), ErrorCode.APPROVAL_REQUIRED
    )
    widen = ask_api(client, tokens.admin, world.prod, "widen_audience", None, {"grants": org_wide})
    assert decide_as(client, tokens, widen.json()["id"], world.approver).status_code == 200
    assert_problem(
        put(client, world, world.prod, tokens.admin, org_wide, version), ErrorCode.APPROVAL_REQUIRED
    )
    exceed = ask_exceed(client, tokens.admin, world, org_wide)
    assert decide_as(client, tokens, exceed, world.member).status_code == 200
    assert put(client, world, world.prod, tokens.admin, org_wide, version).status_code == 200


def test_approving_a_data_source_creates_no_grant(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    finance(client, world, tokens, None, classification="internal")
    asked = ask_api(client, tokens.builder, world.prod, subject="finance")
    assert decide_as(client, tokens, asked.json()["id"], world.approver).status_code == 200
    assert rows(dsns.app, world, "select count(*) from ssc.connection_grant") == [(0,)]
    assert links_of(client, world, world.prod, tokens.admin) == []


def test_an_agent_that_widens_past_a_ceiling_waits_for_both_approvals(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    group = add_group(dsns.app, world, [world.builder])
    finance(client, world, tokens, ceiling_doc(("group", group)))
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    before, version = prod_grants(client, world, tokens)
    org_wide = [*before, ORG_GRANT]
    pending = put(client, world, world.prod, tokens.admin_agent, org_wide, version)
    assert pending.status_code == 202, pending.text
    kinds = sorted(r["kind"] for r in approvals_of(dsns.app, world.org))
    assert kinds == ["agent_share", "exceed_ceiling"]
    assert len(pending.json()["approval_ids"]) == 2
    assert current(client, world, world.prod, tokens.admin)["grants_version"] == version
    for apr in pending.json()["approval_ids"]:
        assert decide_as(client, tokens, apr, world.approver).status_code == 200
    applied = put(client, world, world.prod, tokens.admin_agent, org_wide, version)
    assert applied.status_code == 200, applied.text


def test_linking_an_environment_already_over_the_ceiling_waits_for_the_owner(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    group = add_group(dsns.app, world, [world.builder])
    finance(client, world, tokens, ceiling_doc(("group", group)))
    before, version = prod_grants(client, world, tokens)
    wide = [*before, ORG_GRANT]
    assert put(client, world, world.prod, tokens.admin, wide, version).status_code == 200
    assert_problem(link(client, world, world.prod, tokens.admin), ErrorCode.APPROVAL_REQUIRED)
    assert rows(dsns.app, world, "select count(*) from ssc.connection_grant") == [(0,)]
    apr = ask_exceed(client, tokens.admin, world, wide)
    assert decide_as(client, tokens, apr, world.member).status_code == 200
    linked = link(client, world, world.prod, tokens.admin)
    assert linked.status_code == 200, linked.text
    (granted,) = events_of(dsns.app, world.org, AuditAction.CONNECTION_GRANTED)
    assert granted["policy_decision_id"] is not None
    assert [c["connection"]["name"] for c in links_of(client, world, world.prod, tokens.admin)] == [
        "finance"
    ]
    assert link(client, world, world.preview, tokens.admin).status_code == 200


def test_lowering_a_ceiling_flags_every_environment_now_over_it(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    finance(client, world, tokens, None, classification="internal")
    org_wide = [ORG_GRANT]
    assert put(client, world, world.prod, tokens.admin, org_wide, 1).status_code == 200
    assert put(client, world, world.preview, tokens.admin, [BUILDER_ORG], 1).status_code == 200
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    assert link(client, world, world.preview, tokens.admin).status_code == 200
    quiet_app = new_id("app")
    quiet = new_id("env")
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, world.org)
        conn.execute(
            "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, 'quiet', %s)",
            (quiet_app, world.org, world.admin),
        )
        conn.execute(
            "insert into ssc.environment (id, org_id, app_id, name) values (%s, %s, %s, 'prod')",
            (quiet, world.org, quiet_app),
        )
    assert (
        client.put(
            f"/v1/apps/{quiet_app}/environments/{quiet}/connections/finance",
            json={},
            headers=auth(tokens.admin),
        ).status_code
        == 200
    )
    group = add_group(dsns.app, world, [world.builder])
    lowered = patch(client, tokens.admin, "finance", {"ceiling": ceiling_doc(("group", group))})
    assert lowered.status_code == 200, lowered.text
    assert links_of(client, world, world.prod, tokens.admin)[0]["over_ceiling_since"] is not None
    assert links_of(client, world, world.preview, tokens.admin)[0]["over_ceiling_since"] is not None
    quiet_links = client.get(
        f"/v1/apps/{quiet_app}/environments/{quiet}/connections", headers=auth(tokens.admin)
    ).json()["connections"]
    assert quiet_links[0]["over_ceiling_since"] is None
    flagged = events_of(dsns.app, world.org, AuditAction.CONNECTION_FLAGGED)
    assert len(flagged) == 2
    assert {e["after"]["environment_id"] for e in flagged} == {world.prod, world.preview}
    assert len(events_of(dsns.app, world.org, AuditAction.CONNECTION_CEILING_LOWERED)) == 1
    assert approvals_of(dsns.app, world.org) == []
    same = patch(client, tokens.admin, "finance", {"ceiling": ceiling_doc(("group", group))})
    assert same.status_code == 200
    assert len(events_of(dsns.app, world.org, AuditAction.CONNECTION_FLAGGED)) == 2
    narrowed = put(client, world, world.prod, tokens.admin, [], 2)
    assert narrowed.status_code == 200, narrowed.text
    assert links_of(client, world, world.prod, tokens.admin)[0]["over_ceiling_since"] is None
    assert links_of(client, world, world.preview, tokens.admin)[0]["over_ceiling_since"] is not None
    raised = patch(client, tokens.admin, "finance", {"ceiling": {"audience": "org"}})
    assert raised.status_code == 200, raised.text
    assert links_of(client, world, world.preview, tokens.admin)[0]["over_ceiling_since"] is None
    assert len(events_of(dsns.app, world.org, AuditAction.CONNECTION_CEILING_LOWERED)) == 1


def test_a_flag_clears_when_the_audience_is_narrowed_inside_the_ceiling(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    finance(client, world, tokens, None, classification="internal")
    before, version = prod_grants(client, world, tokens)
    assert (
        put(client, world, world.prod, tokens.admin, [*before, ORG_GRANT], version).status_code
        == 200
    )
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    group = add_group(dsns.app, world, [world.builder])
    assert (
        patch(
            client, tokens.admin, "finance", {"ceiling": ceiling_doc(("group", group))}
        ).status_code
        == 200
    )
    assert links_of(client, world, world.prod, tokens.admin)[0]["over_ceiling_since"] is not None
    assert put(client, world, world.prod, tokens.admin, before, version + 1).status_code == 200
    assert links_of(client, world, world.prod, tokens.admin)[0]["over_ceiling_since"] is None
    wide = [*before, ORG_GRANT]
    assert_problem(
        put(client, world, world.prod, tokens.admin, wide, version + 2), ErrorCode.APPROVAL_REQUIRED
    )
    apr = ask_exceed(client, tokens.admin, world, wide)
    assert decide_as(client, tokens, apr, world.member).status_code == 200
    assert put(client, world, world.prod, tokens.admin, wide, version + 2).status_code == 200
    assert links_of(client, world, world.prod, tokens.admin)[0]["over_ceiling_since"] is None


def test_only_the_owner_or_an_admin_decides_an_exceed_request(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    group = add_group(dsns.app, world, [world.builder])
    finance(client, world, tokens, ceiling_doc(("group", group)))
    before, _ = prod_grants(client, world, tokens)
    wide = [*before, ORG_GRANT]
    apr = ask_exceed(client, tokens.admin, world, wide)
    refusals = [
        (world.admin, tokens.operator, ErrorCode.SELF_APPROVAL_REFUSED),
        (world.member, tokens.operator_agent, ErrorCode.AGENT_SESSION_REFUSED),
        (world.builder, tokens.operator, ErrorCode.APPROVER_NOT_ELIGIBLE),
        (world.retired, tokens.operator, ErrorCode.APPROVER_NOT_ELIGIBLE),
    ]
    for approver, token, code in refusals:
        assert_problem(decide_as(client, tokens, apr, approver, token), code)
    assert decide_as(client, tokens, apr, world.member).status_code == 200


def test_an_admin_decides_an_exceed_request_and_a_plain_request_still_needs_one(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    group = add_group(dsns.app, world, [world.builder])
    finance(client, world, tokens, ceiling_doc(("group", group)))
    before, _ = prod_grants(client, world, tokens)
    apr = ask_exceed(client, tokens.admin, world, [*before, ORG_GRANT])
    assert decide_as(client, tokens, apr, world.approver).status_code == 200
    asked = ask_api(client, tokens.admin, world.prod, subject="finance")
    assert_problem(
        decide_as(client, tokens, asked.json()["id"], world.member),
        ErrorCode.APPROVER_NOT_ELIGIBLE,
    )


def test_an_exceed_request_names_a_connection_that_exists(
    client: TestClient, world: World, tokens: Tokens
) -> None:
    r = ask_api(
        client,
        tokens.admin,
        world.prod,
        "exceed_ceiling",
        None,
        {"connection": "nowhere", "grants": [ORG_GRANT]},
    )
    assert_problem(r, ErrorCode.NOT_FOUND)


def compiled(dsn: str, w: World) -> dict[str, Any]:
    async def work(conn: Any) -> Any:
        return await compile_document(conn, w.org, version=1, compiled_at=datetime.now(UTC))

    doc = asyncio.run(in_org(dsn, w.org, work))
    return doc.model_dump(mode="json")


def test_the_snapshot_carries_ready_connections_with_their_grants(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    assert compiled(dsns.app, world).get("connections") is None
    finance(client, world, tokens, None, classification="internal")
    finance_two = create_connection(
        client, tokens.admin, world.member, "hr", classification="internal"
    )
    assert finance_two.status_code == 201
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    assert link(client, world, world.prod, tokens.admin, "hr").status_code == 200
    assert compiled(dsns.app, world).get("connections") is None
    ready(client, tokens)
    ready(client, tokens, "hr")
    capped = client.put(
        link_path(world, world.preview, "finance"),
        json={"limits": {"max_rows": 10}},
        headers=auth(tokens.admin),
    )
    assert capped.status_code == 200, capped.text
    doc = compiled(dsns.app, world)
    assert list(doc["connections"]) == ["finance", "hr"]
    finance_doc = doc["connections"]["finance"]
    assert finance_doc["status"] == "active"
    assert set(finance_doc["grants"]) == {world.prod, world.preview}
    assert finance_doc["grants"][world.prod].get("limits") is None
    assert finance_doc["grants"][world.preview]["limits"]["max_rows"] == 10
    assert doc["ceiling"] is None
    assert patch(client, tokens.admin, "hr", {"status": "suspended"}).status_code == 200
    assert compiled(dsns.app, world)["connections"]["hr"]["status"] == "suspended"
    assert patch(client, tokens.admin, "hr", {"setup_status": "pending"}).status_code == 200
    assert list(compiled(dsns.app, world)["connections"]) == ["finance"]


def test_a_change_marks_the_snapshot_dirty(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    finance(client, world, tokens, None, classification="internal")
    jobs = "select count(*) from procrastinate.procrastinate_jobs where args->>'org_id' = %s"
    before = rows(dsns.app, world, jobs, (world.org,))[0][0]
    assert patch(client, tokens.admin, "finance", {"status": "suspended"}).status_code == 200
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    assert rows(dsns.app, world, jobs, (world.org,))[0][0] >= before


def test_only_an_active_admin_in_a_person_session_changes_connections(
    client: TestClient, world: World, tokens: Tokens
) -> None:
    body = connection_body(owner_user_id=world.member)
    assert_problem(post(client, "/v1/connections", tokens.builder, body), ErrorCode.FORBIDDEN)
    assert_problem(
        post(client, "/v1/connections", tokens.admin_agent, body), ErrorCode.AGENT_SESSION_REFUSED
    )
    assert post(client, "/v1/connections", tokens.admin, body).status_code == 201
    assert_problem(
        patch(client, tokens.builder, "finance", {"status": "suspended"}), ErrorCode.FORBIDDEN
    )
    assert_problem(
        patch(client, tokens.admin_agent, "finance", {"status": "suspended"}),
        ErrorCode.AGENT_SESSION_REFUSED,
    )
    assert_problem(link(client, world, world.prod, tokens.builder), ErrorCode.FORBIDDEN)
    assert_problem(
        link(client, world, world.prod, tokens.admin_agent), ErrorCode.AGENT_SESSION_REFUSED
    )
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    gone = link_path(world, world.prod, "finance")
    assert_problem(client.delete(gone, headers=auth(tokens.builder)), ErrorCode.FORBIDDEN)
    assert_problem(
        client.delete(gone, headers=auth(tokens.admin_agent)), ErrorCode.AGENT_SESSION_REFUSED
    )
    assert client.delete(gone, headers=auth(tokens.admin)).status_code == 200
    assert_problem(client.delete(gone, headers=auth(tokens.admin)), ErrorCode.NOT_FOUND)


def test_names_are_unique_and_the_owner_is_an_active_user(
    client: TestClient, world: World, tokens: Tokens
) -> None:
    assert create_connection(client, tokens.admin, world.member).status_code == 201
    assert_problem(create_connection(client, tokens.admin, world.member), ErrorCode.ALREADY_EXISTS)
    assert_problem(
        create_connection(client, tokens.admin, world.retired, "hr"), ErrorCode.OWNER_NOT_ACTIVE
    )
    assert_problem(
        create_connection(client, tokens.admin, "usr_" + "z" * 20, "hr"), ErrorCode.OWNER_NOT_ACTIVE
    )
    assert_problem(
        patch(client, tokens.admin, "finance", {"owner_user_id": world.retired}),
        ErrorCode.OWNER_NOT_ACTIVE,
    )
    assert_problem(
        patch(client, tokens.admin, "nowhere", {"status": "suspended"}), ErrorCode.NOT_FOUND
    )
    moved = patch(client, tokens.admin, "finance", {"owner_user_id": world.builder})
    assert moved.json()["owner_user_id"] == world.builder


def test_a_confidential_or_restricted_connection_needs_a_ceiling(
    client: TestClient, world: World, tokens: Tokens
) -> None:
    for classification in ("confidential", "restricted"):
        refused = create_connection(
            client, tokens.admin, world.member, classification, classification=classification
        )
        assert_problem(refused, ErrorCode.CEILING_REQUIRED)
    plain = create_connection(client, tokens.admin, world.member)
    assert plain.status_code == 201
    assert plain.json()["ceiling"] == {"audience": "org", "subjects": []}
    assert plain.json()["setup_status"] == "pending"
    assert_problem(
        patch(client, tokens.admin, "finance", {"classification": "restricted"}),
        ErrorCode.CEILING_REQUIRED,
    )
    ok = patch(
        client,
        tokens.admin,
        "finance",
        {"classification": "restricted", "ceiling": ceiling_doc(("user", world.member))},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["ceiling"] == {
        "audience": "subjects",
        "subjects": [{"kind": "user", "id": world.member}],
    }


def test_the_address_is_stored_and_never_returned(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    made = create_connection(client, tokens.admin, world.member)
    assert made.status_code == 201
    shown = [
        made.text,
        client.get("/v1/connections", headers=auth(tokens.admin)).text,
        client.get("/v1/connections/finance", headers=auth(tokens.admin)).text,
        client.get("/v1/org/deployment-policy", headers=auth(tokens.admin)).text,
    ]
    for text in shown:
        assert "db.corp.internal" not in text
        assert "warehouse" not in text
        assert "5432" not in text
    stored = rows(dsns.app, world, "select host, port, database_name from ssc.connection")
    assert stored == [("db.corp.internal", 5432, "warehouse")]
    audit = events_of(dsns.app, world.org, AuditAction.CONNECTION_CREATED)
    assert "db.corp.internal" not in str(audit)


def test_invalid_connections_are_refused(client: TestClient, world: World, tokens: Tokens) -> None:
    bad: list[dict[str, Any]] = [
        {"name": "Finance"},
        {"port": 0},
        {"port": 70000},
        {"host": "db corp"},
        {"kind": "oracle"},  # not a kind; an unavailable kind is CONNECTOR_UNAVAILABLE (GA-5)
        {"kind": "gsheets"},  # a kind whose address is not host/port/database
        {"classification": "secret"},
        {"allowed_schemas": []},
        {"ceiling": {"audience": "subjects", "subjects": []}},
        {"ceiling": {"audience": "org", "subjects": [{"kind": "user", "id": USER}]}},
        {"ceiling": {"audience": "subjects", "subjects": [{"kind": "group", "id": USER}]}},
        {"surprise": 1},
    ]
    for change in bad:
        body = {**connection_body(owner_user_id=world.member), **change}
        r = post(client, "/v1/connections", tokens.admin, body)
        assert r.status_code == 422, (change, r.text)
        assert r.json()["code"] == ErrorCode.VALIDATION_FAILED.value
    assert client.get("/v1/connections", headers=auth(tokens.admin)).json() == {"connections": []}


def test_connections_stay_in_their_org(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns, signing_key: SigningKey
) -> None:
    assert create_connection(client, tokens.admin, world.member).status_code == 201
    other = asyncio.run(make_world(dsns.app, "Elsewhere"))
    theirs = mint(signing_key, org=other.org, sub=other.admin, jti=f"cred_{new_key()[:16]}")
    assert client.get("/v1/connections", headers=auth(theirs)).json() == {"connections": []}
    assert_problem(client.get("/v1/connections/finance", headers=auth(theirs)), ErrorCode.NOT_FOUND)
    assert_problem(patch(client, theirs, "finance", {"status": "suspended"}), ErrorCode.NOT_FOUND)
    assert create_connection(client, theirs, other.member).status_code == 201


def test_connections_are_seen_by_the_approvals_rule(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    for name in ("finance", "hr", "payroll"):
        assert create_connection(client, tokens.admin, world.member, name).status_code == 201
        assert link(client, world, world.prod, tokens.admin, name).status_code == 200
    asked = ask_api(client, tokens.builder, world.prod, subject="finance")
    assert decide_as(client, tokens, asked.json()["id"], world.approver).status_code == 200
    ask_api(client, tokens.builder, world.prod, subject="hr")

    def names(token: str) -> list[str]:
        r = client.get("/v1/connections", headers=auth(token))
        assert r.status_code == 200, r.text
        return [c["name"] for c in r.json()["connections"]]

    def linked(token: str) -> list[str]:
        return [c["connection"]["name"] for c in links_of(client, world, world.prod, token)]

    everything = ["finance", "hr", "payroll"]
    for token in (tokens.admin, tokens.approver, tokens.admin_agent, tokens.operator):
        assert names(token) == everything
        assert linked(token) == everything
    assert names(tokens.builder) == ["finance"]
    assert linked(tokens.builder) == ["finance"]
    assert names(tokens.member) == []
    assert linked(tokens.member) == []
    assert client.get("/v1/connections/finance", headers=auth(tokens.builder)).status_code == 200
    assert_problem(
        client.get("/v1/connections/hr", headers=auth(tokens.builder)), ErrorCode.NOT_FOUND
    )
    assert_problem(
        client.get("/v1/connections/finance", headers=auth(tokens.member)), ErrorCode.NOT_FOUND
    )
    policy = client.get("/v1/org/deployment-policy", headers=auth(tokens.builder)).json()
    assert [c["name"] for c in policy["connections"]] == ["finance"]
    audited = events_of(dsns.app, world.org, AuditAction.OPERATOR_ACCESS)
    assert len(audited) >= 1


# GA-5: kinds and addresses.


def test_a_connection_has_a_kind_and_only_an_available_kind_is_created(
    client: TestClient, world: World, tokens: Tokens
) -> None:
    made = create_connection(client, tokens.admin, world.member)
    assert made.status_code == 201
    assert made.json()["kind"] == "postgres"
    for kind, address in [
        ("sqlserver", {"host": "sql.corp.internal", "database": "shop"}),
        ("bigquery", {"project": "corp-analytics", "dataset": "warehouse"}),
        ("airtable", {"base_id": "appA1b2C3d4E5f6G7"}),
    ]:
        body = {
            "name": f"src-{kind}",
            "kind": kind,
            "owner_user_id": world.member,
            "classification": "internal",
            "address": address,
        }
        r = post(client, "/v1/connections", tokens.admin, body)
        assert_problem(r, ErrorCode.CONNECTOR_UNAVAILABLE)
    listed = client.get("/v1/connections", headers=auth(tokens.admin)).json()["connections"]
    assert [c["name"] for c in listed] == ["finance"]


def test_an_address_is_the_kind_s_and_is_sent_one_way(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    base = {"owner_user_id": world.member, "classification": "internal"}
    by_address = post(
        client,
        "/v1/connections",
        tokens.admin,
        {"name": "ledger", "address": {"host": "db.corp.internal", "database": "ledger"}, **base},
    )
    assert by_address.status_code == 201, by_address.text
    stored = rows(dsns.app, world, "select host, port, database_name, address from ssc.connection")
    assert stored == [
        (
            "db.corp.internal",
            5432,
            "ledger",
            {"host": "db.corp.internal", "port": 5432, "database": "ledger"},
        )
    ]
    assert "db.corp.internal" not in by_address.text
    bad: list[dict[str, Any]] = [
        {
            "name": "a",
            "host": "db.corp.internal",
            "database": "x",
            "address": {"host": "h", "database": "x"},
        },
        {"name": "b", "address": {"host": "db.corp.internal"}},
        {"name": "c", "address": {"host": "db.corp.internal", "database": "x", "user": "u"}},
        {
            "name": "d",
            "address": {"spreadsheet_id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"},
        },
        {"name": "e", "kind": "nosql", "address": {"host": "h", "database": "x"}},
        {"name": "f"},
    ]
    for body in bad:
        r = post(client, "/v1/connections", tokens.admin, {**base, **body})
        assert r.status_code == 422, (body, r.text)
        assert r.json()["code"] == ErrorCode.VALIDATION_FAILED.value
        assert "db.corp.internal" not in r.text


def test_the_snapshot_names_the_kind_of_every_ready_connection(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    finance(client, world, tokens, None, classification="internal")
    ready(client, tokens)
    assert link(client, world, world.prod, tokens.admin).status_code == 200
    doc = compiled(dsns.app, world)
    # postgres is the kind before GA-5, so it is left out and a document keeps its bytes.
    assert "kind" not in doc["connections"]["finance"]
    assert SnapshotDoc.model_validate(doc).connections["finance"].kind == "postgres"
