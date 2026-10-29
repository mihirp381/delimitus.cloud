"""CD-4.2: the customer domain API, the confirmed flip and the operator retire, against
postgres:18, the fake runtime and the fake cell agent."""

from __future__ import annotations

import secrets
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
import test_deploy
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from ssc_testkit import (
    Dsns,
    SigningKey,
    assert_problem,
    auth,
    make_org,
    mint,
    new_key,
    with_api_cell,
)
from test_app_identity_env import SETTINGS
from test_cell_resources import jobs
from test_deploy import Bench, build_release, deploy, execute, get, rows_of
from test_domains_tick import resolvers

from ssc_contracts.domains import resource_name
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.api.routes.v1 import domains as domain_routes
from ssc_control.api.settings import INTERNAL_AUDIENCE
from ssc_control.db import bound_org
from ssc_control.db.domains import suffix_list
from ssc_control.domains.probe import DOH_ENDPOINTS, FakeResolver, ProbeError, Resolver
from ssc_control.domains.provider import OTHER, parent_provider, provider_of
from ssc_control.domains.tick import tick
from ssc_control.runtime.cell_domains import FakeCellDomains
from ssc_control.runtime.cells import STATIC_LABEL, OrgCell, StaticCells
from ssc_control.runtime.driver import AppIdentity
from ssc_control.runtime.reconciler import reconcile_env
from ssc_control.snapshot.service import compile_lock
from ssc_control.worker import app_identity_from_env

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b

APPS = "delimitusapps.com"
PARENT_NS = frozenset({"ada.ns.cloudflare.com", "bob.ns.cloudflare.com"})


@pytest.fixture
def rival(dsns: Dsns) -> str:
    return make_org(dsns.app, "Rival").org_id


class Broken(FakeResolver):
    async def ns(self, name: str) -> frozenset[str]:
        raise ProbeError("down")


def name(b: Bench, first: str = "tools") -> str:
    return f"{first}-{b.w.org[4:]}.testorg.com"


def label(b: Bench) -> str:
    return str(rows_of(b.dsn, b.w.org, "select cell_label from ssc.org")[0]["cell_label"])


def default(b: Bench) -> str:
    return f"{label(b)}.{APPS}"


def app_of(b: Bench) -> FastAPI:
    return cast("FastAPI", b.client.app)


def cell(b: Bench, *, identity: AppIdentity | None = None) -> FakeCellDomains:
    fake = FakeCellDomains()
    app = app_of(b)
    with_api_cell(app, domains=fake, identity=identity, runtime=b.runtime)
    app.state.runtime = replace(app.state.runtime, resolvers=resolvers({"testorg.com": PARENT_NS}))
    return fake


async def made(b: Bench, fake: FakeCellDomains) -> Any:
    """The domain tick's first pass: the cell makes the zone of each new row."""
    cells = StaticCells(OrgCell(label=STATIC_LABEL, domains=fake))
    await tick(b.ports.engine, cells, resolvers({"testorg.com": PARENT_NS}), datetime.now(UTC))
    return get(b, "/v1/domain", b.t.admin).json()["delegation"]


def put(b: Bench, suffix: str, token: str) -> Any:
    return b.client.put("/v1/domain", json={"suffix": suffix}, headers=auth(token))


def flip(b: Bench, to: str, token: str, key: str | None = None, **body: Any) -> Any:
    return b.client.post(
        "/v1/domain/flip",
        json={"to": to, "confirm": True, **body},
        headers=auth(token, **{IDEMPOTENCY_HEADER: key or new_key()}),
    )


def domain_audit(b: Bench) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select action, actor_id, target_id, before, after from ssc.audit_event "
        "where action like 'domain.%%' order by seq",
    )


def set_state(b: Bench, state: str) -> None:
    execute(b.dsn, b.w.org, "update ssc.org_domain set state = %s where state <> 'retired'", state)


def identity_for(b: Bench) -> AppIdentity:
    found = app_identity_from_env(SETTINGS)
    assert found is not None
    return replace(found, cell_label=label(b))


async def converge(b: Bench, identity: AppIdentity) -> None:
    async with bound_org(b.ports.engine, b.w.org) as conn:
        apps_suffix = (await conn.execute(text("select apps_suffix from ssc.org"))).scalar()
        previous = await suffix_list(conn, b.w.org, apps_suffix)
    now = replace(identity, suffix=apps_suffix, previous_suffixes=previous)
    for _ in range(4):
        out = await reconcile_env(
            b.ports.engine,
            b.runtime,
            b.ports.release_specs,
            org_id=b.w.org,
            env_id=b.w.preview,
            identity=now,
        )
        if out.kind == "converged":
            return
    raise AssertionError("the environment did not converge")


# ── provider ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hosts", "want"),
    [
        (["ada.ns.cloudflare.com"], "cloudflare"),
        (["ns-12.awsdns-01.com", "ns-900.awsdns-40.net"], "route53"),
        (["ns51.domaincontrol.com"], "godaddy"),
        (["ns1-01.azure-dns.com"], "azure"),
        (["ns-cloud-a1.googledomains.com"], "google"),
        (["ns1.google.com"], "google"),
        (["dns1.p01.nsone.net"], "ns1"),
        (["ns1.example.net"], OTHER),
        ([], OTHER),
    ],
)
def test_the_provider_is_named_from_the_name_servers(hosts: list[str], want: str) -> None:
    assert provider_of(hosts) == want


async def test_the_provider_is_read_from_the_nearest_zone_above() -> None:
    answers = {"acme.co.uk": frozenset({"ns51.domaincontrol.com"})}
    assert await parent_provider(resolvers(answers), "apps.tools.acme.co.uk") == "godaddy"
    assert await parent_provider(resolvers({}), "apps.acme.com") is None
    broken: dict[str, Resolver] = {n: Broken() for n in DOH_ENDPOINTS}
    assert await parent_provider(broken, "apps.acme.com") is None


# ── who may ──────────────────────────────────────────────────────────────────


async def test_members_and_agent_sessions_cannot_change_the_domain(
    b: Bench, signing_key: SigningKey
) -> None:
    fake = cell(b)
    assert_problem(get(b, "/v1/domain", b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(put(b, name(b), b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(flip(b, "customer", b.t.member), ErrorCode.FORBIDDEN)
    agent = mint(signing_key, org=b.w.org, sub=b.w.admin, jti=f"cred_{new_key()[:16]}", agent=True)
    assert get(b, "/v1/domain", agent).status_code == 200
    assert_problem(put(b, name(b), agent), ErrorCode.AGENT_SESSION_REFUSED)
    assert_problem(flip(b, "customer", agent), ErrorCode.AGENT_SESSION_REFUSED)
    assert fake.calls == []
    assert domain_audit(b) == []


async def test_admission_refuses_what_cannot_be_the_org_s_suffix(b: Bench) -> None:
    fake = cell(b)
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.org_suffix_retired (org_id, suffix, until) values (%s, %s, now())",
        b.w.org,
        name(b, "old"),
    )
    refusals = {
        "localhost": ErrorCode.DOMAIN_SUFFIX_TOO_SHORT,
        "UPPER.testorg.com": ErrorCode.VALIDATION_FAILED,
        default(b): ErrorCode.DOMAIN_SUFFIX_RESERVED,
        "apps.delimitus.com": ErrorCode.DOMAIN_SUFFIX_RESERVED,
        "tools.co.uk": ErrorCode.DOMAIN_SUFFIX_PUBLIC,
        name(b, "old"): ErrorCode.DOMAIN_SUFFIX_OWN,
    }
    for suffix, code in refusals.items():
        assert_problem(put(b, suffix, b.t.admin), code)
    execute(b.dsn, b.w.org, "update ssc.org set apps_suffix = %s", name(b, "mine"))
    assert_problem(put(b, name(b, "mine"), b.t.admin), ErrorCode.DOMAIN_SUFFIX_OWN)
    assert fake.calls == []


# ── asking for a domain ──────────────────────────────────────────────────────


async def test_an_admin_asks_for_a_domain_once(b: Bench) -> None:
    fake = cell(b)
    r = put(b, name(b), b.t.admin)
    assert r.status_code == 200, r.text
    body = r.json()
    d = body["delegation"]
    assert (d["suffix"], d["state"], d["provider"], d["name_servers"]) == (
        name(b),
        "pending_dns",
        None,
        [],
    )
    assert fake.calls == []
    d = await made(b, fake)
    assert (d["state"], d["provider"]) == ("pending_dns", "cloudflare")
    assert d["name_servers"] == list(fake.name_servers)
    assert (d["observed_ns"], d["failure"], d["last_delegated_at"]) == (None, None, None)
    assert (body["default_suffix"], body["current_suffix"]) == (default(b), default(b))
    assert (body["ready_to_flip"], body["rolling"], body["previous"]) == (False, 0, [])
    assert body["apps_that_move"] == [
        {"slug": "ledger", "hosts": [f"ledger.{name(b)}", f"ledger--preview.{name(b)}"]}
    ]
    (requested,) = domain_audit(b)
    assert (requested["action"], requested["actor_id"], requested["target_id"]) == (
        "domain.requested",
        b.w.admin,
        d["id"],
    )
    assert requested["after"] == {
        "suffix": name(b),
        "state": "pending_dns",
        "zone_name": resource_name(name(b)),
    }

    again = put(b, name(b), b.t.admin)
    assert again.status_code == 200
    assert again.json()["delegation"]["id"] == d["id"]
    assert len(domain_audit(b)) == 1
    assert fake.calls == [("create", name(b))]
    assert_problem(put(b, name(b, "other"), b.t.admin), ErrorCode.ALREADY_EXISTS)
    assert get(b, "/v1/domain", b.t.admin).json()["delegation"] == d


async def test_a_failed_domain_is_replaced(b: Bench) -> None:
    fake = cell(b)
    put(b, name(b), b.t.admin)
    first = await made(b, fake)
    set_state(b, "failed")
    execute(b.dsn, b.w.org, "update ssc.org_domain set failure = 'no delegation seen'")
    again = put(b, name(b), b.t.admin).json()["delegation"]
    assert (again["id"], again["state"], again["failure"]) == (first["id"], "pending_dns", None)
    assert (again["name_servers"], (await made(b, fake))["name_servers"]) == (
        [],
        list(fake.name_servers),
    )
    set_state(b, "failed")
    other = put(b, name(b, "other"), b.t.admin).json()["delegation"]
    assert (other["suffix"], other["state"]) == (name(b, "other"), "pending_dns")
    await made(b, fake)
    assert fake.calls == [("create", name(b)), ("create", name(b)), ("create", name(b, "other"))]
    states = rows_of(b.dsn, b.w.org, "select suffix, state from ssc.org_domain order by suffix")
    assert states == [
        {"suffix": name(b, "other"), "state": "pending_dns"},
        {"suffix": name(b), "state": "retired"},
    ]
    assert [a["action"] for a in domain_audit(b)] == [
        "domain.requested",
        "domain.requested",
        "domain.retired",
        "domain.requested",
    ]
    set_state(b, "failed")
    assert_problem(put(b, name(b), b.t.admin), ErrorCode.DOMAIN_SUFFIX_OWN)


async def test_a_suffix_another_org_holds_already_exists(b: Bench, rival: str) -> None:
    cell(b)
    execute(
        b.dsn,
        rival,
        "insert into ssc.org_domain (id, org_id, suffix, zone_name) values (%s, %s, %s, %s)",
        new_id("dom"),
        rival,
        name(b),
        f"ssc-d-{secrets.token_hex(6)}",
    )
    assert_problem(put(b, name(b), b.t.admin), ErrorCode.ALREADY_EXISTS)
    assert_problem(put(b, f"eu.{name(b)}", b.t.admin), ErrorCode.DOMAIN_SUFFIX_HELD)
    assert rows_of(b.dsn, b.w.org, "select id from ssc.org_domain") == []
    assert domain_audit(b) == []


async def test_a_name_another_org_asked_for_and_never_delegated_is_free_once_it_failed(
    b: Bench, rival: str
) -> None:
    cell(b)
    execute(
        b.dsn,
        rival,
        "insert into ssc.org_domain (id, org_id, suffix, zone_name, state) "
        "values (%s, %s, %s, %s, 'failed')",
        new_id("dom"),
        rival,
        name(b),
        resource_name(name(b)),
    )
    r = put(b, name(b), b.t.admin)
    assert r.status_code == 200, r.text
    assert r.json()["delegation"]["state"] == "pending_dns"


async def test_a_failed_name_asked_again_after_another_org_took_it_already_exists(
    b: Bench, rival: str
) -> None:
    cell(b)
    put(b, name(b), b.t.admin)
    set_state(b, "failed")
    execute(
        b.dsn,
        rival,
        "insert into ssc.org_domain (id, org_id, suffix, zone_name) values (%s, %s, %s, %s)",
        new_id("dom"),
        rival,
        name(b),
        resource_name(name(b)),
    )
    assert_problem(put(b, name(b), b.t.admin), ErrorCode.ALREADY_EXISTS)
    assert rows_of(b.dsn, b.w.org, "select state from ssc.org_domain") == [{"state": "failed"}]


async def test_a_cell_without_the_domains_port_is_unavailable(b: Bench) -> None:
    with_api_cell(app_of(b), domains=None)
    assert_problem(put(b, name(b), b.t.admin), ErrorCode.CELL_UNAVAILABLE)
    assert rows_of(b.dsn, b.w.org, "select id from ssc.org_domain") == []


# ── the flip ─────────────────────────────────────────────────────────────────


async def test_there_is_nothing_to_flip_to_without_an_active_domain(b: Bench) -> None:
    cell(b)
    assert_problem(flip(b, "customer", b.t.admin), ErrorCode.DOMAIN_STATE)
    assert_problem(flip(b, "default", b.t.admin), ErrorCode.DOMAIN_STATE)
    put(b, name(b), b.t.admin)
    assert_problem(flip(b, "customer", b.t.admin), ErrorCode.DOMAIN_STATE)
    r: Any = b.client.post(
        "/v1/domain/flip",
        json={"to": "customer", "confirm": False},
        headers=auth(b.t.admin, **{IDEMPOTENCY_HEADER: new_key()}),
    )
    assert_problem(r, ErrorCode.VALIDATION_FAILED)


async def test_the_flip_waits_for_every_app_to_roll_and_moves_them_both_ways(b: Bench) -> None:
    identity = identity_for(b)
    release = await build_release(b, b.w.preview)
    assert (await deploy(b, b.w.preview, release))[1] == "healthy"
    await converge(b, identity)
    cell(b, identity=identity)
    put(b, name(b), b.t.admin)
    set_state(b, "active")

    waiting = get(b, "/v1/domain", b.t.admin).json()
    assert (waiting["ready_to_flip"], waiting["rolling"]) == (False, 1)
    refused = flip(b, "customer", b.t.admin)
    assert_problem(refused, ErrorCode.DOMAIN_NOT_READY)
    assert rows_of(b.dsn, b.w.org, "select apps_suffix from ssc.org")[0]["apps_suffix"] is None

    await converge(b, identity)
    ready = get(b, "/v1/domain", b.t.admin).json()
    assert (ready["ready_to_flip"], ready["rolling"]) == (True, 0)
    published = len(jobs(b, compile_lock(b.w.org)))
    key = new_key()
    r = flip(b, "customer", b.t.admin, key)
    assert r.status_code == 200, r.text
    moved = r.json()
    assert moved["current_suffix"] == name(b)
    (prev,) = moved["previous"]
    assert prev["suffix"] == default(b)
    until = datetime.fromisoformat(prev["until"])
    assert abs(until - (datetime.now(UTC) + timedelta(days=30))) < timedelta(minutes=1)
    assert moved["apps_that_move"][0]["hosts"][0] == f"ledger.{default(b)}"
    assert flip(b, "customer", b.t.admin, key).json() == moved
    flipped = [a for a in domain_audit(b) if a["action"] == "domain.flipped"]
    assert len(flipped) == 1
    assert (flipped[0]["actor_id"], flipped[0]["before"], flipped[0]["after"]) == (
        b.w.admin,
        {"suffix": default(b)},
        {"suffix": name(b), "to": "customer"},
    )
    assert len(jobs(b, compile_lock(b.w.org))) >= max(published, 1)
    envs = get(b, f"/v1/apps/{b.w.app}", b.t.admin).json()["environments"]
    assert {e["name"]: e["url"] for e in envs} == {
        "prod": f"https://ledger.{name(b)}",
        "preview": f"https://ledger--preview.{name(b)}",
    }

    back = get(b, "/v1/domain", b.t.admin).json()
    assert (back["ready_to_flip"], back["rolling"]) == (False, 1)
    assert_problem(flip(b, "default", b.t.admin), ErrorCode.DOMAIN_NOT_READY)
    await converge(b, identity)
    r = flip(b, "default", b.t.admin)
    assert r.status_code == 200, r.text
    assert r.json()["current_suffix"] == default(b)
    assert [p["suffix"] for p in r.json()["previous"]] == [name(b)]
    retired = rows_of(b.dsn, b.w.org, "select suffix from ssc.org_suffix_retired")
    assert retired == [{"suffix": name(b)}]
    assert rows_of(b.dsn, b.w.org, "select apps_suffix from ssc.org")[0]["apps_suffix"] is None
    assert [a["after"]["to"] for a in domain_audit(b) if a["action"] == "domain.flipped"] == [
        "customer",
        "default",
    ]


async def test_the_flip_counts_rolling_apps_before_it_locks_the_org(
    b: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def rolling(_c: Any, engine: AsyncEngine, *_a: Any, org_id: str) -> int:
        async with bound_org(engine, org_id) as other:
            await other.execute(
                text("select 1 from ssc.org where id = :org for no key update nowait"),
                {"org": org_id},
            )
        return 0

    monkeypatch.setattr(domain_routes, "rolling_for", rolling)
    cell(b)
    put(b, name(b), b.t.admin)
    set_state(b, "active")
    r = flip(b, "customer", b.t.admin)
    assert r.status_code == 200, r.text
    assert rows_of(b.dsn, b.w.org, "select apps_suffix from ssc.org")[0]["apps_suffix"] == name(b)


async def test_two_flips_wait_for_the_org_lock_without_deadlocking(b: Bench, dsns: Dsns) -> None:
    cell(b)
    put(b, name(b), b.t.admin)
    set_state(b, "active")
    with ThreadPoolExecutor(2) as pool:
        with psycopg.connect(dsns.superuser) as held:
            held.execute("select 1 from ssc.org where id = %s for no key update", (b.w.org,))
            calls = [pool.submit(flip, b, "customer", b.t.admin) for _ in range(2)]
            done, _ = wait(calls, timeout=1)
            assert not done
        answers = sorted(call.result(timeout=10).status_code for call in calls)
    assert answers == [200, 409]
    flipped = [a for a in domain_audit(b) if a["action"] == "domain.flipped"]
    assert len(flipped) == 1


# ── operator retire ──────────────────────────────────────────────────────────


def operator(key: SigningKey, org: str, kind: str = "operator", sub: str = "op_dana") -> str:
    return mint(
        key, org=org, sub=sub, kind=kind, audience=INTERNAL_AUDIENCE, jti=f"cred_{new_key()[:16]}"
    )


def retire(b: Bench, org: str, token: str) -> Any:
    return b.client.post(
        f"/internal/v1/orgs/{org}/domain/retire",
        headers=auth(token, **{IDEMPOTENCY_HEADER: new_key()}),
    )


async def test_an_operator_retires_a_domain_its_parent_no_longer_delegates(
    b: Bench, signing_key: SigningKey, rival: str
) -> None:
    fake = cell(b)
    op = operator(signing_key, b.w.org)
    assert_problem(retire(b, b.w.org, op), ErrorCode.NOT_FOUND)
    workload = operator(signing_key, b.w.org, kind="workload", sub=b.w.preview)
    assert_problem(retire(b, b.w.org, workload), ErrorCode.FORBIDDEN)
    assert_problem(retire(b, rival, op), ErrorCode.FORBIDDEN)

    d = put(b, name(b), b.t.admin).json()["delegation"]
    execute(b.dsn, b.w.org, "update ssc.org set apps_suffix = %s", name(b))
    execute(
        b.dsn,
        b.w.org,
        "update ssc.org_domain set observed_ns = %s::jsonb",
        '{"google": ["ada.ns.cloudflare.com"]}',
    )
    assert_problem(retire(b, b.w.org, op), ErrorCode.DOMAIN_STATE)
    execute(b.dsn, b.w.org, "update ssc.org set apps_suffix = null")
    r = retire(b, b.w.org, op)
    assert r.status_code == 200, r.text
    assert (r.json()["id"], r.json()["state"]) == (d["id"], "retired")
    assert fake.calls[-1] == ("retire", name(b))
    last = domain_audit(b)[-1]
    assert (last["action"], last["actor_id"]) == ("domain.retired", "op_dana")
    assert get(b, "/v1/domain", b.t.admin).json()["delegation"] is None


async def test_retire_asks_the_resolvers_again_and_refuses_while_the_parent_delegates(
    b: Bench, signing_key: SigningKey, caplog: pytest.LogCaptureFixture
) -> None:
    fake = cell(b)
    op = operator(signing_key, b.w.org)
    put(b, name(b), b.t.admin)
    ours = frozenset((await made(b, fake))["name_servers"])
    gone = '{"cloudflare": [], "google": [], "quad9": []}'
    execute(b.dsn, b.w.org, "update ssc.org_domain set observed_ns = %s::jsonb", gone)
    app = app_of(b)
    app.state.runtime = replace(app.state.runtime, resolvers=resolvers({name(b): ours}))
    r = retire(b, b.w.org, op)
    assert_problem(r, ErrorCode.DOMAIN_STATE)
    assert "still delegates" in caplog.text

    app.state.runtime = replace(app.state.runtime, resolvers={n: Broken() for n in DOH_ENDPOINTS})
    r = retire(b, b.w.org, op)
    assert_problem(r, ErrorCode.DOMAIN_STATE)
    assert "did not all answer" in caplog.text
    assert ("retire", name(b)) not in fake.calls

    app.state.runtime = replace(app.state.runtime, resolvers=resolvers({}))
    execute(b.dsn, b.w.org, "update ssc.org_domain set observed_ns = null")
    r = retire(b, b.w.org, op)
    assert r.status_code == 200, r.text
    assert fake.calls[-1] == ("retire", name(b))
    seen = rows_of(b.dsn, b.w.org, "select observed_ns from ssc.org_domain")[0]["observed_ns"]
    assert seen == {n: [] for n in DOH_ENDPOINTS}
