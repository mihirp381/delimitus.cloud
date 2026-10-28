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

from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.db import bound_org, make_engine
from ssc_control.db.orgs import NewOrg, create_org
from ssc_control.domains.admission import LOCK_CLASS, admit

APPS = "delimitusapps.com"
HELD = ErrorCode.DOMAIN_SUFFIX_HELD
SHORT = ErrorCode.DOMAIN_SUFFIX_TOO_SHORT
PUBLIC = ErrorCode.DOMAIN_SUFFIX_PUBLIC
RESERVED = ErrorCode.DOMAIN_SUFFIX_RESERVED


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


async def admitted(o: Orgs, suffix: str, org: str | None = None) -> ErrorCode | None:
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
    ("suffix", "code"),
    [
        ("example.com", SHORT),
        ("localhost", SHORT),
        ("Tools.example.com", ErrorCode.VALIDATION_FAILED),
        ("tools.delimitus.com", RESERVED),
        ("a.b.delimitusapps.com", RESERVED),
        ("x.kawasaki.jp", PUBLIC),
        ("k12.ak.us", PUBLIC),
        ("tools.co.uk", PUBLIC),
        ("a.x.kawasaki.jp", PUBLIC),
        ("city.kawasaki.jp", PUBLIC),
        ("tools.xn--55qx5d.cn", PUBLIC),
    ],
)
async def test_a_suffix_that_cannot_be_a_customer_suffix_is_refused(
    o: Orgs, suffix: str, code: ErrorCode
) -> None:
    assert await admitted(o, suffix) == code


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
    assert await admitted(o, held) == HELD
    assert await admitted(o, o.base) == HELD
    assert await admitted(o, f"eu.{held}") == HELD
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
    assert await admitted(o, old) == HELD
    assert await admitted(o, f"apps.{o.base}") == HELD
    assert await admitted(o, f"x.{old}") == HELD


async def test_another_org_s_requested_domain_is_refused_above_and_below(o: Orgs) -> None:
    asked = f"tools.{o.base}"
    await hold_domain(o, o.theirs, asked)
    assert await admitted(o, asked) is None
    assert await admitted(o, o.base) == HELD
    assert await admitted(o, f"eu.{asked}") == HELD


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
    assert await asyncio.wait_for(rival, 10) == HELD
