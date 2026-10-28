"""CD-5.3: suffix admission, against postgres:18 and the vendored Public Suffix List."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from ssc_testkit import ISSUER, Dsns

from ssc_contracts.ids import new_id
from ssc_control.db import bound_org, make_engine
from ssc_control.db.orgs import NewOrg, create_org
from ssc_control.domains.admission import LOCK_CLASS, admit

APPS = "delimitusapps.com"
ABOVE = "the suffix is above a suffix another organization uses or used"
BELOW = "the suffix is under a suffix another organization uses or used"
TAKEN = "another organization uses or used the suffix"


@dataclass(frozen=True)
class Orgs:
    engine: AsyncEngine
    mine: str
    theirs: str
    base: str


async def new_org(engine: AsyncEngine, name: str) -> str:
    spec = NewOrg(name, "Ada Admin", "ada@example.com", ISSUER, new_id("usr"))
    return (await create_org(engine, spec)).org_id


@pytest.fixture
async def o(dsns: Dsns) -> AsyncIterator[Orgs]:
    engine = make_engine(dsns.app)
    mine, theirs = await new_org(engine, "Mine"), await new_org(engine, "Theirs")
    yield Orgs(engine, mine, theirs, f"t{secrets.token_hex(5)}.testorg.com")
    await engine.dispose()


async def admitted(o: Orgs, suffix: str, org: str | None = None) -> str | None:
    async with bound_org(o.engine, org or o.mine) as conn:
        return await admit(conn, org or o.mine, suffix, apps_domain=APPS)


async def hold(o: Orgs, org: str, sql: str, **params: object) -> None:
    async with bound_org(o.engine, org) as conn:
        await conn.execute(text(sql), {"org": org, **params})


async def hold_domain(o: Orgs, org: str, suffix: str) -> None:
    await hold(
        o,
        org,
        "insert into ssc.org_domain (id, org_id, suffix, zone_name) "
        "values (:id, :org, :suffix, :zone)",
        id=new_id("dom"),
        suffix=suffix,
        zone=f"ssc-d-{secrets.token_hex(6)}",
    )


@pytest.mark.parametrize(
    ("suffix", "reason"),
    [
        ("example.com", "a suffix needs at least three labels, such as tools.example.com"),
        ("localhost", "a suffix needs at least three labels, such as tools.example.com"),
        ("Tools.example.com", "not a usable apps suffix: 'Tools.example.com'"),
        ("tools.delimitus.com", "the suffix is under a Delimitus domain"),
        ("a.b.delimitusapps.com", "the suffix is under a Delimitus domain"),
        ("x.kawasaki.jp", "the suffix is a public suffix"),
        ("k12.ak.us", "the suffix is a public suffix"),
        ("tools.co.uk", "the suffix is a registered domain; ask for a name under it"),
        ("a.x.kawasaki.jp", "the suffix is a registered domain; ask for a name under it"),
        ("city.kawasaki.jp", "the suffix is a registered domain; ask for a name under it"),
        ("tools.xn--55qx5d.cn", "the suffix is a registered domain; ask for a name under it"),
    ],
)
async def test_a_suffix_that_cannot_be_a_customer_suffix_is_refused(
    o: Orgs, suffix: str, reason: str
) -> None:
    assert await admitted(o, suffix) == reason


@pytest.mark.parametrize(
    "suffix", ["apps.tools.co.uk", "tools.city.kawasaki.jp", "tools.testorg.github.io"]
)
async def test_a_name_under_a_registered_domain_is_admitted(o: Orgs, suffix: str) -> None:
    assert await admitted(o, f"t{secrets.token_hex(5)}.{suffix}") is None


async def test_another_org_s_current_suffix_is_refused_with_its_ancestors_and_descendants(
    o: Orgs,
) -> None:
    held = f"tools.{o.base}"
    await hold_domain(o, o.theirs, held)
    await hold(o, o.theirs, "update ssc.org set apps_suffix = :s where id = :org", s=held)
    assert await admitted(o, held) == TAKEN
    assert await admitted(o, o.base) == ABOVE
    assert await admitted(o, f"eu.{held}") == BELOW
    assert await admitted(o, f"crm.{o.base}") is None
    assert await admitted(o, f"eu.{held}", o.theirs) is None


async def test_another_org_s_previous_suffix_is_refused_with_its_ancestors_and_descendants(
    o: Orgs,
) -> None:
    old = f"old.apps.{o.base}"
    await hold(
        o,
        o.theirs,
        "insert into ssc.org_suffix_retired (org_id, suffix, until) values (:org, :s, now())",
        s=old,
    )
    assert await admitted(o, old) == TAKEN
    assert await admitted(o, f"apps.{o.base}") == ABOVE
    assert await admitted(o, f"x.{old}") == BELOW


async def test_another_org_s_requested_domain_is_refused_above_and_below(o: Orgs) -> None:
    asked = f"tools.{o.base}"
    await hold_domain(o, o.theirs, asked)
    assert await admitted(o, asked) is None
    assert await admitted(o, o.base) == ABOVE
    assert await admitted(o, f"eu.{asked}") == BELOW


async def test_the_check_holds_one_lock_until_the_caller_commits(o: Orgs) -> None:
    asked = f"tools.{o.base}"
    async with bound_org(o.engine, o.mine) as conn:
        assert await admit(conn, o.mine, asked, apps_domain=APPS) is None
        locks = text(
            "select count(*) from pg_locks where locktype = 'advisory' and granted "
            "and classid = :cls and objid = 0 and pid = pg_backend_pid()"
        )
        assert (await conn.execute(locks, {"cls": LOCK_CLASS})).scalar_one() == 1
        rival = asyncio.create_task(admitted(o, f"eu.{asked}", o.theirs))
        await asyncio.sleep(0.5)
        assert not rival.done()
        await conn.execute(
            text(
                "insert into ssc.org_domain (id, org_id, suffix, zone_name) "
                "values (:id, :org, :suffix, :zone)"
            ),
            {
                "id": new_id("dom"),
                "org": o.mine,
                "suffix": asked,
                "zone": f"ssc-d-{secrets.token_hex(6)}",
            },
        )
    assert await asyncio.wait_for(rival, 10) == BELOW
