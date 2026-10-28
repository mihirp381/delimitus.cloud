import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from ssc_testkit import Dsns, make_org

from ssc_control.db import bind_org_sync, bound_org, make_engine
from ssc_control.db.domains import OrgDomain, domain_of, insert_domain, open_domains
from ssc_control.domains import jobs
from ssc_control.domains.probe import DOH_ENDPOINTS, FakeResolver, ProbeError, Resolver
from ssc_control.domains.tick import NO_DELEGATION, STALLED_MARKER, tick
from ssc_control.runtime.cell_domains import FAKE_NAME_SERVERS, FakeCellDomains
from ssc_control.runtime.cells import STATIC_LABEL, OrgCell, StaticCells
from ssc_control.worker_ports import Ports

OTHER_NS = frozenset({"ada.ns.cloudflare.com", "bob.ns.cloudflare.com"})


class BrokenFor(FakeResolver):
    def __init__(self, broken: str, answers: dict[str, frozenset[str]]) -> None:
        super().__init__(answers)
        self.broken = broken

    async def ns(self, name: str) -> frozenset[str]:
        if name == self.broken:
            raise ProbeError("resolver down")
        return await super().ns(name)


def resolvers(answers: dict[str, frozenset[str]]) -> dict[str, Resolver]:
    return {name: FakeResolver(dict(answers)) for name in DOH_ENDPOINTS}


def audit(dsn: str, org: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        return conn.execute(
            "select action, actor_kind, before, after from ssc.audit_event "
            "where action like 'domain.%' order by seq"
        ).fetchall()


@pytest.fixture
def org(dsns: Dsns) -> str:
    return make_org(dsns.app).org_id


@pytest.fixture
def other(dsns: Dsns) -> str:
    return make_org(dsns.app).org_id


@pytest.fixture
async def engine(dsns: Dsns) -> Any:
    made = make_engine(dsns.app)
    yield made
    await made.dispose()


async def requested(engine: AsyncEngine, org: str, fake: FakeCellDomains) -> OrgDomain:
    suffix = f"tools.{org[4:]}.com"
    made = await fake.create(suffix)
    async with bound_org(engine, org) as conn:
        return await insert_domain(
            conn,
            org_id=org,
            suffix=suffix,
            zone_name=made.zone_name,
            name_servers=made.name_servers,
            certificate=made.certificate,
        )


async def current(engine: AsyncEngine, org: str) -> OrgDomain:
    async with bound_org(engine, org) as conn:
        row = await domain_of(conn, org)
    assert row is not None
    return row


def cells(fake: FakeCellDomains) -> StaticCells:
    return StaticCells(OrgCell(label=STATIC_LABEL, domains=fake))


async def test_a_domain_moves_from_pending_to_active(
    dsns: Dsns, org: str, engine: AsyncEngine
) -> None:
    fake = FakeCellDomains()
    row = await requested(engine, org, fake)
    assert row.id in {r.id for r in await open_domains(engine)}
    now = datetime.now(UTC)
    wrong = resolvers({row.suffix: OTHER_NS})
    report = await tick(engine, cells(fake), wrong, now)
    assert row.id not in report.changed
    seen = await current(engine, org)
    assert seen.state == "pending_dns"
    assert seen.observed_ns == {k: tuple(sorted(OTHER_NS)) for k in DOH_ENDPOINTS}
    right = resolvers({row.suffix: frozenset(FAKE_NAME_SERVERS)})
    report = await tick(engine, cells(fake), right, now)
    assert row.id in report.changed
    seen = await current(engine, org)
    assert (seen.state, seen.last_delegated_at) == ("issuing", now)
    fake.advance(row.suffix, "provisioning", "tools: AUTHORIZING")
    await tick(engine, cells(fake), right, now + timedelta(minutes=1))
    seen = await current(engine, org)
    assert (seen.state, seen.failure) == ("issuing", "tools: AUTHORIZING")
    fake.advance(row.suffix, "active")
    report = await tick(engine, cells(fake), right, now + timedelta(minutes=2))
    assert row.id in report.changed
    seen = await current(engine, org)
    assert (seen.state, seen.failure) == ("active", None)
    assert row.id not in {r.id for r in await open_domains(engine)}
    assert audit(dsns.app, org) == [
        (
            "domain.delegated",
            "operator",
            {"suffix": row.suffix, "state": "pending_dns"},
            {
                "suffix": row.suffix,
                "state": "issuing",
                "name_servers": list(FAKE_NAME_SERVERS),
            },
        ),
        (
            "domain.active",
            "operator",
            {"suffix": row.suffix, "state": "issuing"},
            {"suffix": row.suffix, "state": "active"},
        ),
    ]


async def test_a_failed_certificate_fails_the_domain_with_its_detail(
    dsns: Dsns, org: str, engine: AsyncEngine
) -> None:
    fake = FakeCellDomains()
    row = await requested(engine, org, fake)
    right = resolvers({row.suffix: frozenset(FAKE_NAME_SERVERS)})
    now = datetime.now(UTC)
    await tick(engine, cells(fake), right, now)
    fake.advance(row.suffix, "failed", "AUTHORIZATION_ISSUE no CNAME")
    await tick(engine, cells(fake), right, now)
    seen = await current(engine, org)
    assert (seen.state, seen.failure) == ("failed", "AUTHORIZATION_ISSUE no CNAME")
    assert audit(dsns.app, org)[-1][0] == "domain.failed"
    assert audit(dsns.app, org)[-1][3]["failure"] == "AUTHORIZATION_ISSUE no CNAME"


async def test_a_certificate_the_cell_lost_fails_the_domain(org: str, engine: AsyncEngine) -> None:
    fake = FakeCellDomains()
    row = await requested(engine, org, fake)
    right = resolvers({row.suffix: frozenset(FAKE_NAME_SERVERS)})
    await tick(engine, cells(fake), right, datetime.now(UTC))
    await fake.retire(row.suffix)
    await tick(engine, cells(fake), right, datetime.now(UTC))
    seen = await current(engine, org)
    assert (seen.state, seen.failure) == ("failed", "the cell has no certificate for the domain")


async def test_no_delegation_in_72_hours_fails_the_domain(
    dsns: Dsns, org: str, engine: AsyncEngine
) -> None:
    fake = FakeCellDomains()
    row = await requested(engine, org, fake)
    wrong = resolvers({row.suffix: OTHER_NS})
    await tick(engine, cells(fake), wrong, row.created_at + timedelta(hours=71, minutes=59))
    assert (await current(engine, org)).state == "pending_dns"
    await tick(engine, cells(fake), wrong, row.created_at + timedelta(hours=72))
    seen = await current(engine, org)
    assert (seen.state, seen.failure) == ("failed", NO_DELEGATION)
    assert seen.observed_ns == {k: tuple(sorted(OTHER_NS)) for k in DOH_ENDPOINTS}
    assert audit(dsns.app, org) == [
        (
            "domain.failed",
            "operator",
            {"suffix": row.suffix, "state": "pending_dns"},
            {"suffix": row.suffix, "state": "failed", "failure": NO_DELEGATION},
        )
    ]


async def test_a_stalled_certificate_is_logged_once_and_still_polled(
    org: str, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeCellDomains()
    row = await requested(engine, org, fake)
    right = resolvers({row.suffix: frozenset(FAKE_NAME_SERVERS)})
    now = datetime.now(UTC)
    await tick(engine, cells(fake), right, now)
    caplog.set_level(logging.ERROR, logger="ssc.domains")
    report = await tick(engine, cells(fake), right, now + timedelta(minutes=119))
    assert row.id not in report.stalled
    for minutes in (120, 121, 180):
        report = await tick(engine, cells(fake), right, now + timedelta(minutes=minutes))
        assert (row.id in report.stalled) == (minutes == 120)
    marked = [r for r in caplog.records if getattr(r, "marker", None) == STALLED_MARKER]
    assert [(r.levelno, r.domain_id) for r in marked] == [(logging.ERROR, row.id)]  # pyright: ignore[reportAttributeAccessIssue]
    seen = await current(engine, org)
    assert seen.state == "issuing" and seen.stalled_at == now + timedelta(minutes=120)
    fake.advance(row.suffix, "active")
    await tick(engine, cells(fake), right, now + timedelta(minutes=181))
    assert (await current(engine, org)).state == "active"


async def test_one_row_that_fails_does_not_stop_the_others(
    org: str, other: str, engine: AsyncEngine
) -> None:
    fake = FakeCellDomains()
    broken = await requested(engine, org, fake)
    fine = await requested(engine, other, fake)
    answers = {fine.suffix: frozenset(FAKE_NAME_SERVERS)}
    mixed: dict[str, Resolver] = {
        "cloudflare": BrokenFor(broken.suffix, answers),
        "google": FakeResolver(answers),
        "quad9": FakeResolver(answers),
    }
    report = await tick(engine, cells(fake), mixed, datetime.now(UTC))
    assert fine.id in report.changed
    assert (await current(engine, org)).state == "pending_dns"
    assert (await current(engine, other)).state == "issuing"


async def test_an_issuing_row_waits_while_its_cell_cannot_be_reached(
    org: str, engine: AsyncEngine
) -> None:
    fake = FakeCellDomains()
    row = await requested(engine, org, fake)
    right = resolvers({row.suffix: frozenset(FAKE_NAME_SERVERS)})
    await tick(engine, cells(fake), right, datetime.now(UTC))
    fake.advance(row.suffix, "active")
    await tick(engine, StaticCells(), right, datetime.now(UTC))
    await tick(engine, None, right, datetime.now(UTC))
    assert (await current(engine, org)).state == "issuing"


async def test_the_job_needs_resolvers(engine: AsyncEngine) -> None:
    assert await jobs.tick_with(Ports(engine=engine)) is None
    report = await jobs.tick_with(Ports(engine=engine, resolvers=resolvers({})))
    assert report is not None and report.errors == ()
