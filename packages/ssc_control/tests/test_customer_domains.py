import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import psycopg
import pytest
from ssc_testkit import Dsns, make_org

from ssc_contracts.audit import ActorKind
from ssc_contracts.domains import resource_name
from ssc_control.audit import Actor
from ssc_control.db import bind_org_sync, bound_org, make_engine
from ssc_control.db.domains import (
    OrgDomain,
    domain_of,
    domains_in_state,
    insert_domain,
    update_state,
)
from ssc_control.domains.lifecycle import RetireRefusedError, retire, retire_allowed
from ssc_control.domains.probe import (
    DOH_ENDPOINTS,
    DohResolver,
    FakeResolver,
    ProbeError,
    delegation_gone,
    delegation_seen,
    doh_resolvers,
    observe,
)
from ssc_control.runtime.cell_domains import (
    AgentCellDomains,
    CellDomainNotFoundError,
    CellDomainsError,
    FakeCellDomains,
)
from ssc_shared.runtime import ORG_HEADER

ORG = "org_" + "c" * 20
SUFFIX = "tools.testorg.com"
ZONE = resource_name(SUFFIX)
CERT = f"projects/ssc-c-test/locations/global/certificates/{ZONE}"
NS = frozenset({"ns-cloud-a1.googledomains.com", "ns-cloud-a2.googledomains.com"})
NOW = datetime(2026, 10, 28, tzinfo=UTC)
OPERATOR = Actor(kind=ActorKind.OPERATOR, id="op_dana")


async def tokens(url: str) -> str:
    return f"id-token-for-{url}"


def agent_answering(answers: dict[str, httpx2.Response], seen: list[httpx2.Request]) -> Any:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return answers[request.url.path.rsplit("/", 1)[1]]

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return AgentCellDomains("https://agent.test/", tokens, org_id=ORG, client=client)


async def test_the_agent_client_sends_the_suffix_and_checks_the_answers() -> None:
    seen: list[httpx2.Request] = []
    agent = agent_answering(
        {
            "create": httpx2.Response(
                200,
                json={
                    "zone_name": ZONE,
                    "name_servers": [
                        "NS-Cloud-A1.googledomains.com.",
                        "ns-cloud-a2.googledomains.com",
                    ],
                    "certificate": CERT,
                },
            ),
            "status": httpx2.Response(
                200, json={"name_servers": [], "state": "active", "detail": None}
            ),
            "retire": httpx2.Response(200, json={"retired": SUFFIX}),
        },
        seen,
    )
    made = await agent.create(SUFFIX)
    assert (made.zone_name, set(made.name_servers), made.certificate) == (ZONE, NS, CERT)
    assert (await agent.status(SUFFIX)).state == "active"
    await agent.retire(SUFFIX)
    assert [r.url.path for r in seen] == [
        f"/v1/domains/{m}" for m in ("create", "status", "retire")
    ]
    for request in seen:
        assert request.headers["authorization"] == "Bearer id-token-for-https://agent.test"
        assert request.headers[ORG_HEADER] == ORG
        assert json.loads(request.content) == {"suffix": SUFFIX}
    with pytest.raises(ValueError, match="suffix"):
        await agent.create("testorg.com")


@pytest.mark.parametrize(
    ("method", "answer"),
    [
        ("create", {"zone_name": "ssc-d-000000000000", "name_servers": [], "certificate": CERT}),
        (
            "create",
            {"zone_name": ZONE, "name_servers": [], "certificate": "projects/x/certificates/y"},
        ),
        ("create", {"zone_name": ZONE, "name_servers": ["not a host"], "certificate": CERT}),
        ("create", {"zone_name": ZONE, "name_servers": ["a.b"] * 17, "certificate": CERT}),
        ("status", {"name_servers": [], "state": "ACTIVE", "detail": None}),
        ("status", {"name_servers": [], "state": "failed", "detail": ""}),
        ("status", {"name_servers": "a.b", "state": "failed", "detail": None}),
        ("retire", {"retired": "other.testorg.com"}),
    ],
)
async def test_the_agent_client_refuses_an_answer_it_cannot_trust(
    method: str, answer: dict[str, Any]
) -> None:
    agent = agent_answering({method: httpx2.Response(200, json=answer)}, [])
    with pytest.raises(CellDomainsError):
        await getattr(agent, method)(SUFFIX)


async def test_a_missing_domain_is_its_own_error() -> None:
    missing = httpx2.Response(404, json={"code": "DOMAIN_NOT_FOUND", "message": "no zone"})
    agent = agent_answering({"status": missing}, [])
    with pytest.raises(CellDomainNotFoundError):
        await agent.status(SUFFIX)
    off = httpx2.Response(503, json={"code": "DOMAINS_NOT_CONFIGURED", "message": "off"})
    agent = agent_answering({"create": off}, [])
    with pytest.raises(CellDomainsError, match="DOMAINS_NOT_CONFIGURED") as e:
        await agent.create(SUFFIX)
    assert not isinstance(e.value, CellDomainNotFoundError)


async def test_the_fake_cell_advances_a_domain() -> None:
    fake = FakeCellDomains()
    with pytest.raises(CellDomainNotFoundError):
        await fake.status(SUFFIX)
    made = await fake.create(SUFFIX)
    assert made.zone_name == ZONE
    assert (await fake.status(SUFFIX)).state == "provisioning"
    fake.advance(SUFFIX, "failed", "CAA")
    assert ((await fake.status(SUFFIX)).state, (await fake.status(SUFFIX)).detail) == (
        "failed",
        "CAA",
    )
    await fake.retire(SUFFIX)
    with pytest.raises(CellDomainNotFoundError):
        await fake.status(SUFFIX)


def doh(answer: httpx2.Response, seen: list[httpx2.Request]) -> DohResolver:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return answer

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return DohResolver(DOH_ENDPOINTS["quad9"], client=client)


async def test_doh_reads_the_ns_answer() -> None:
    seen: list[httpx2.Request] = []
    body = {
        "Status": 0,
        "Answer": [
            {"name": f"{SUFFIX}.", "type": 2, "TTL": 300, "data": "NS-Cloud-A1.googledomains.com."},
            {"name": f"{SUFFIX}.", "type": 2, "TTL": 300, "data": "ns-cloud-a2.googledomains.com"},
            {"name": f"{SUFFIX}.", "type": 46, "TTL": 300, "data": "NS 8 3 ..."},
        ],
    }
    assert await doh(httpx2.Response(200, json=body), seen).ns(SUFFIX) == NS
    (request,) = seen
    assert str(request.url).startswith("https://dns.quad9.net:5053/dns-query?")
    assert dict(request.url.params) == {"name": SUFFIX, "type": "NS"}
    assert request.headers["accept"] == "application/dns-json"
    for empty in ({"Status": 3}, {"Status": 0}, {"Status": 0, "Answer": []}):
        assert await doh(httpx2.Response(200, json=empty), []).ns(SUFFIX) == frozenset()


@pytest.mark.parametrize(
    "answer",
    [
        httpx2.Response(200, json={"Status": 2}),
        httpx2.Response(500, json={"Status": 0}),
        httpx2.Response(200, text="not json"),
        httpx2.Response(200, json=[]),
        httpx2.Response(200, json={"Status": 0, "Answer": {}}),
    ],
)
async def test_a_resolver_that_fails_never_reads_as_gone(answer: httpx2.Response) -> None:
    with pytest.raises(ProbeError):
        await doh(answer, []).ns(SUFFIX)


async def test_the_three_resolvers_are_asked() -> None:
    assert set(doh_resolvers()) == {"cloudflare", "google", "quad9"}
    assert DOH_ENDPOINTS["cloudflare"] == "https://cloudflare-dns.com/dns-query"
    assert DOH_ENDPOINTS["google"] == "https://dns.google/resolve"
    resolvers = {"a": FakeResolver({SUFFIX: NS}), "b": FakeResolver()}
    assert await observe(resolvers, SUFFIX) == {"a": NS, "b": frozenset()}


def test_the_delegation_is_seen_when_every_resolver_answers_ours() -> None:
    upper = frozenset(f"{h.upper()}." for h in NS)
    assert delegation_seen({"a": NS, "b": upper, "c": NS}, NS)
    assert not delegation_seen({"a": NS, "b": frozenset(), "c": NS}, NS)
    assert not delegation_seen({"a": NS | {"ns1.other.com"}}, NS)
    assert not delegation_seen({}, NS)
    assert not delegation_seen({"a": frozenset()}, frozenset())
    assert delegation_gone({"a": frozenset(), "b": frozenset({"ns1.other.com"})}, NS)
    assert not delegation_gone(
        {"a": frozenset(), "b": frozenset({"NS-CLOUD-A1.googledomains.com."})}, NS
    )
    assert not delegation_gone({}, NS)


def domain(**change: Any) -> OrgDomain:
    row = OrgDomain(
        id="dom_" + "a" * 20,
        org_id=ORG,
        suffix=SUFFIX,
        zone_name=ZONE,
        name_servers=tuple(sorted(NS)),
        state="active",
        observed_ns={k: tuple(NS) for k in DOH_ENDPOINTS},
        failure=None,
        certificate=CERT,
        last_delegated_at=NOW - timedelta(days=30),
        retire_after=NOW + timedelta(days=330),
        created_at=NOW - timedelta(days=40),
        updated_at=NOW,
    )
    return replace(row, **change)


def test_retiring_waits_for_the_delegation_to_go_or_the_cap() -> None:
    assert retire_allowed(domain(), now=NOW) == f"the parent still delegates {SUFFIX} to its zone"
    partly = {"cloudflare": (), "google": (), "quad9": tuple(NS)}
    assert retire_allowed(domain(observed_ns=partly), now=NOW) is not None
    gone = {k: () for k in DOH_ENDPOINTS}
    assert retire_allowed(domain(observed_ns=gone), now=NOW) is None
    elsewhere = {k: ("ns1.other.com",) for k in DOH_ENDPOINTS}
    assert retire_allowed(domain(observed_ns=elsewhere), now=NOW) is None
    assert retire_allowed(domain(observed_ns=None), now=NOW) == (
        "no probe has answered for the domain yet"
    )
    capped = NOW + timedelta(days=330)
    assert retire_allowed(domain(), now=capped) is None
    assert retire_allowed(domain(observed_ns=None), now=capped) is None
    assert retire_allowed(domain(state="retired"), now=capped) == "the domain is already retired"


def rows(dsn: str, org: str, sql: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        return conn.execute(sql).fetchall()


@pytest.fixture
def org(dsns: Dsns) -> str:
    return make_org(dsns.app).org_id


async def test_a_domain_row_is_kept_and_retired_once_its_delegation_is_gone(
    dsns: Dsns, org: str
) -> None:
    engine = make_engine(dsns.app)
    suffix = f"tools.{org[4:]}.com"
    try:
        async with bound_org(engine, org) as conn:
            row = await insert_domain(
                conn, org_id=org, suffix=suffix, zone_name=resource_name(suffix)
            )
            assert (row.state, row.name_servers, row.observed_ns) == ("pending_dns", (), None)
            assert row.retire_after - row.created_at >= timedelta(days=365)
            assert await domain_of(conn, org) == row
        delegated = datetime.now(UTC)
        async with bound_org(engine, org) as conn:
            row = await update_state(
                conn,
                org_id=org,
                domain_id=row.id,
                state="active",
                observed_ns={k: list(NS) for k in DOH_ENDPOINTS},
                last_delegated_at=delegated,
                name_servers=sorted(NS),
                certificate=CERT,
            )
            assert (row.state, set(row.name_servers), row.certificate) == ("active", NS, CERT)
            assert row.last_delegated_at == delegated
            assert row.retire_after.year == delegated.year + 1
            assert [r.id for r in await domains_in_state(conn, org, "active")] == [row.id]
        fake = FakeCellDomains()
        await fake.create(suffix)
        async with bound_org(engine, org) as conn:
            with pytest.raises(RetireRefusedError, match="still delegates"):
                await retire(conn, fake, row, OPERATOR, datetime.now(UTC))
        assert fake.calls == [("create", suffix)]
        async with bound_org(engine, org) as conn:
            gone = {k: [] for k in DOH_ENDPOINTS}
            row = await update_state(
                conn, org_id=org, domain_id=row.id, state="retiring", observed_ns=gone
            )
            assert row.last_delegated_at == delegated
            done = await retire(conn, fake, row, OPERATOR, datetime.now(UTC))
            assert done.state == "retired"
            assert await domain_of(conn, org) is None
        assert fake.calls[-1] == ("retire", suffix)
        assert rows(dsns.app, org, "select suffix from ssc.org_suffix_retired") == [(suffix,)]
        audit = rows(
            dsns.app,
            org,
            "select action, target_kind, target_id, before, after from ssc.audit_event "
            "where action like 'domain.%'",
        )
        assert audit == [
            (
                "domain.retired",
                "org_domain",
                row.id,
                {"suffix": suffix, "state": "retiring"},
                {"suffix": suffix, "state": "retired", "zone_name": resource_name(suffix)},
            )
        ]
        async with bound_org(engine, org) as conn:
            again = await insert_domain(
                conn,
                org_id=org,
                suffix=f"apps.{org[4:]}.com",
                zone_name=resource_name(f"apps.{org[4:]}.com"),
            )
            assert (await domain_of(conn, org)) == again
    finally:
        await engine.dispose()


async def test_a_domain_never_delegated_is_retired_without_keeping_its_name(
    dsns: Dsns, org: str
) -> None:
    engine = make_engine(dsns.app)
    suffix = f"tools.{org[4:]}.com"
    try:
        async with bound_org(engine, org) as conn:
            row = await insert_domain(
                conn, org_id=org, suffix=suffix, zone_name=resource_name(suffix)
            )
            gone = {k: [] for k in DOH_ENDPOINTS}
            row = await update_state(
                conn, org_id=org, domain_id=row.id, state="failed", observed_ns=gone
            )
            done = await retire(conn, FakeCellDomains(), row, OPERATOR, datetime.now(UTC))
            assert done.state == "retired"
        assert rows(dsns.app, org, "select suffix from ssc.org_suffix_retired") == []
    finally:
        await engine.dispose()
