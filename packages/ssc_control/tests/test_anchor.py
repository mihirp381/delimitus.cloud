"""SSC-012 (A1b): audit anchors in the blob store, the daily anchor job, ``verify --anchors``,
the restore procedure's ``reanchor``, and the ``audit_anchor`` table.

Ticket "done when" checks (lane brief, anchors):
  * the fs BlobStore gets ``audit-anchors/<org>/<date>.json``
                                  -> test_the_daily_anchor_is_one_object_and_one_row
  * ``verify --anchors`` fails when a chain row before the anchor is rewritten
                                  -> test_a_recomputed_history_passes_verify_but_not_its_anchors
Plus: adoption of an orphan object, refusal to replace a contradicting one, tail loss, missing and
malformed objects, the 36-hour rule, re-anchoring after a restore (and refusing a tampered or
wrongly-dated one), the CLI, the job tick, and the table's isolation and immutability.

Revision 0011 also gives every org its cell label (founder default D1)
                                  -> test_every_org_gets_its_own_cell_label_at_creation
                                  -> test_0011_backfills_cell_labels_and_round_trips
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from alembic.script import ScriptDirectory
from procrastinate import App, PsycopgConnector
from sqlalchemy.engine import make_url
from ssc_testkit import Dsns, make_org

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.ids import new_id
from ssc_control.audit import Actor, NewEvent, append_event, canonical_bytes
from ssc_control.audit import jobs as audit_jobs
from ssc_control.audit.__main__ import main, run_verify
from ssc_control.audit.anchor import (
    FORMAT,
    Anchor,
    AnchorConflictError,
    AnchorFormatError,
    AnchorProblem,
    AnchorReport,
    ReanchorRefusedError,
    daily_key,
    parse_anchor,
    reanchor,
    verify_anchors,
    write_anchor,
)
from ssc_control.audit.verify import VerifyReport
from ssc_control.db import (
    MIGRATE_ROLE,
    SqlState,
    bind_org_sync,
    bound_org,
    downgrade,
    make_engine,
    upgrade,
)
from ssc_control.db.errors import INSUFFICIENT_PRIVILEGE
from ssc_control.db.migrate import alembic_config
from ssc_control.worker import build_app, queue_conninfo
from ssc_control.worker_ports import PORTS_KEY, Ports
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.clock import SystemClock

DAY = date(2026, 9, 1)
LATER = date(2026, 9, 2)

# ── helpers ──────────────────────────────────────────────────────────────────


def blob_store(root: Path) -> FsBlobStore:
    clock = SystemClock()
    signer = UrlSigner({"k1": secrets.token_bytes(32)}, active="k1", clock=clock)
    return FsBlobStore(root, signer=signer, base_url="http://blobs.test/blobs", clock=clock)


def event(org: str, n: int) -> NewEvent:
    return NewEvent(
        org_id=org,
        action=AuditAction.SCHEDULE_CREATED,
        actor=Actor(ActorKind.USER, "usr_builder"),
        target_kind="schedule",
        target_id=f"sch_{n}",
        after={"name": f"job-{n}", "cron": "0 * * * *"},
    )


async def append(dsn: str, org: str, count: int) -> None:
    engine = make_engine(dsn)
    try:
        for n in range(count):
            async with bound_org(engine, org) as conn:
                await append_event(conn, event(org, n))
    finally:
        await engine.dispose()


def org_with_events(dsns: Dsns, events: int = 5) -> str:
    """org.created plus ``events`` more."""
    org = make_org(dsns.app, "Anchored").org_id
    asyncio.run(append(dsns.app, org, events))
    return org


async def anchor_of(dsn: str, org: str, blob: FsBlobStore, day: date | None = DAY) -> Anchor:
    engine = make_engine(dsn)
    try:
        async with bound_org(engine, org) as conn:
            return await write_anchor(conn, org, blob, "daily", day=day)
    finally:
        await engine.dispose()


async def check(
    dsn: str, org: str, blob: FsBlobStore, now: datetime, restoring_to: datetime | None = None
) -> tuple[VerifyReport, AnchorReport]:
    engine = make_engine(dsn)
    try:
        return await verify_anchors(engine, org, blob, now=now, restoring_to=restoring_to)
    finally:
        await engine.dispose()


async def read(blob: FsBlobStore, key: str) -> bytes:
    return b"".join([chunk async for chunk in blob.get(key)])


async def keys(blob: FsBlobStore, org: str) -> list[str]:
    return [info.key async for info in blob.list(f"audit-anchors/{org}/")]


def db_now(dsns: Dsns) -> datetime:
    with psycopg.connect(dsns.app) as conn:
        row = conn.execute("select clock_timestamp()").fetchone()
    assert row is not None
    return cast(datetime, row[0])


def rows(dsns: Dsns, org: str, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, org)
        return conn.execute(sql, params).fetchall()


def as_superuser(dsns: Dsns, *statements: tuple[str, tuple[object, ...]]) -> None:
    """Rewrite history with the append-only triggers switched off."""
    with psycopg.connect(dsns.superuser) as conn:
        conn.execute("set session_replication_role = replica")
        for sql, params in statements:
            conn.execute(sql, params)


def recompute_from(dsns: Dsns, org: str, seq: int) -> None:
    """Edit event ``seq`` and recompute every later hash and the head, so ``verify`` passes."""
    with psycopg.connect(dsns.superuser) as conn:
        conn.execute("set session_replication_role = replica")
        found = conn.execute(
            "select seq, canonical, prev_hash from ssc.audit_event "
            "where org_id = %s and seq >= %s order by seq",
            (org, seq),
        ).fetchall()
        prev = bytes(found[0][2])
        for n, canonical, _ in found:
            body = bytes(canonical)
            if n == seq:
                doc = json.loads(body)
                doc["after"] = {**doc["after"], "name": "rewritten"}
                body = canonical_bytes(doc)
                conn.execute(
                    "update ssc.audit_event set after = %s::jsonb where org_id = %s and seq = %s",
                    (json.dumps(doc["after"]), org, n),
                )
            digest = hashlib.sha256(prev + body).digest()
            conn.execute(
                "update ssc.audit_event set canonical = %s, prev_hash = %s, hash = %s "
                "where org_id = %s and seq = %s",
                (body, prev, digest, org, n),
            )
            prev = digest
        conn.execute("update ssc.audit_head set hash = %s where org_id = %s", (prev, org))


def refused(dsn: str, org: str, sql: str, params: tuple[object, ...] = ()) -> str:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        with pytest.raises(psycopg.Error) as e:
            conn.execute(sql, params)
        conn.rollback()
    assert e.value.sqlstate
    return e.value.sqlstate


class RollbackError(Exception):
    pass


# ── writing ──────────────────────────────────────────────────────────────────


def test_the_daily_anchor_is_one_object_and_one_row(dsns: Dsns, tmp_path: Path) -> None:
    org = org_with_events(dsns)
    blob = blob_store(tmp_path)
    anchor = asyncio.run(anchor_of(dsns.app, org, blob))
    assert anchor.object_key == f"audit-anchors/{org}/2026-09-01.json"
    head = rows(dsns, org, "select seq, hash from ssc.audit_head")
    assert (anchor.seq, anchor.hash, anchor.reason, anchor.restored_to) == (
        6,
        bytes(head[0][1]),
        "daily",
        None,
    )
    raw = asyncio.run(read(blob, anchor.object_key))
    assert parse_anchor(anchor.object_key, raw, org_id=org) == anchor
    doc = json.loads(raw)
    assert doc["format"] == FORMAT and doc["hash"] == anchor.hash.hex() and doc["seq"] == 6
    assert raw == canonical_bytes(doc)
    stored = rows(
        dsns, org, "select seq, hash, object_key, reason, restored_to from ssc.audit_anchor"
    )
    assert stored == [(6, anchor.hash, anchor.object_key, "daily", None)]
    # The same day again: the first anchor, nothing new, though the head has moved.
    asyncio.run(append(dsns.app, org, 1))
    assert asyncio.run(anchor_of(dsns.app, org, blob)) == anchor
    assert len(rows(dsns, org, "select 1 from ssc.audit_anchor")) == 1
    assert asyncio.run(read(blob, anchor.object_key)) == raw
    chain, report = asyncio.run(check(dsns.app, org, blob, db_now(dsns)))
    assert chain.ok and report.ok
    assert (report.checked, report.superseded, report.newest) == (1, 0, anchor)


def test_an_orphan_object_is_adopted_not_replaced(dsns: Dsns, tmp_path: Path) -> None:
    org = org_with_events(dsns)
    blob = blob_store(tmp_path)

    async def written_then_rolled_back() -> Anchor:
        engine = make_engine(dsns.app)
        try:
            async with bound_org(engine, org) as conn:
                first = await write_anchor(conn, org, blob, "daily", day=DAY)
                raise RollbackError(first)
        except RollbackError as rolled:
            return cast(Anchor, rolled.args[0])
        finally:
            await engine.dispose()

    orphan = asyncio.run(written_then_rolled_back())
    assert rows(dsns, org, "select 1 from ssc.audit_anchor") == []
    raw = asyncio.run(read(blob, orphan.object_key))
    asyncio.run(append(dsns.app, org, 2))
    adopted = asyncio.run(anchor_of(dsns.app, org, blob))
    assert adopted == orphan and adopted.seq == 6  # the object's head, not the current one
    assert asyncio.run(read(blob, orphan.object_key)) == raw
    assert rows(dsns, org, "select seq from ssc.audit_anchor") == [(6,)]
    chain, report = asyncio.run(check(dsns.app, org, blob, db_now(dsns)))
    assert chain.ok and report.ok


def test_a_contradicting_object_is_refused_and_never_replaced(
    dsns: Dsns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    org = org_with_events(dsns)
    blob = blob_store(tmp_path)
    key = daily_key(org, DAY)
    forged = Anchor(
        org_id=org,
        seq=3,
        hash=hashlib.sha256(b"forged").digest(),
        anchored_at=db_now(dsns),
        reason="daily",
        restored_to=None,
        object_key=key,
    )
    asyncio.run(blob.put(key, forged.document()))
    with pytest.raises(AnchorConflictError, match="disagrees with the chain"):
        asyncio.run(anchor_of(dsns.app, org, blob))
    assert asyncio.run(read(blob, key)) == forged.document()
    assert rows(dsns, org, "select 1 from ssc.audit_anchor") == []
    # The operator command refuses the same way (it writes today's key, so forge that one too).
    today = daily_key(org, db_now(dsns).astimezone(UTC).date())
    asyncio.run(blob.put(today, replace(forged, object_key=today).document()))
    fs_env(monkeypatch, tmp_path, dsns)
    assert main(["anchor", "--org", org]) == 1
    assert capsys.readouterr().out.startswith("refused: ")


def test_a_malformed_object_is_refused(dsns: Dsns, tmp_path: Path) -> None:
    org = org_with_events(dsns, 0)
    blob = blob_store(tmp_path)
    asyncio.run(blob.put(daily_key(org, DAY), b'{"format": "ssc-audit-anchor/v1"}'))
    with pytest.raises(AnchorFormatError):
        asyncio.run(anchor_of(dsns.app, org, blob))


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: {**d, "extra": 1}, id="extra key"),
        pytest.param(lambda d: {**d, "format": "ssc-audit-anchor/v2"}, id="format"),
        pytest.param(lambda d: {**d, "org_id": "org_" + "x" * 20}, id="other org"),
        pytest.param(lambda d: {**d, "seq": -1}, id="negative seq"),
        pytest.param(lambda d: {**d, "seq": True}, id="bool seq"),
        pytest.param(lambda d: {**d, "hash": d["hash"].upper()}, id="uppercase hash"),
        pytest.param(lambda d: {**d, "anchored_at": "2026-09-01T00:05:00"}, id="naive time"),
        pytest.param(lambda d: {**d, "reason": "weekly"}, id="reason"),
        pytest.param(lambda d: {**d, "restored_to": d["anchored_at"]}, id="daily restored_to"),
    ],
)
def test_parse_is_strict(mutate: Any) -> None:
    org = "org_" + "a" * 20
    good = Anchor(
        org_id=org,
        seq=4,
        hash=bytes(range(32)),
        anchored_at=datetime(2026, 9, 1, 0, 5, tzinfo=UTC),
        reason="daily",
        restored_to=None,
        object_key=daily_key(org, DAY),
    )
    raw = good.document()
    assert parse_anchor(good.object_key, raw, org_id=org) == good
    with pytest.raises(AnchorFormatError):
        parse_anchor(good.object_key, raw + b" ", org_id=org)  # not canonical
    with pytest.raises(AnchorFormatError):
        parse_anchor(good.object_key, canonical_bytes(mutate(json.loads(raw))), org_id=org)


# ── verifying ────────────────────────────────────────────────────────────────


def test_a_recomputed_history_passes_verify_but_not_its_anchors(
    dsns: Dsns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    org = org_with_events(dsns)
    blob = blob_store(tmp_path)
    anchor = asyncio.run(anchor_of(dsns.app, org, blob))
    fs_env(monkeypatch, tmp_path, dsns)
    assert main(["verify", "--org", org, "--anchors"]) == 0
    assert capsys.readouterr().out == (
        f"ok: 6 events, head at seq 6\nanchors: 1 checked, 0 superseded, newest seq 6 at "
        f"{anchor.object_key}\n"
    )
    recompute_from(dsns, org, 3)
    assert asyncio.run(run_verify(dsns.app, org)).ok  # the chain alone cannot tell
    chain, report = asyncio.run(check(dsns.app, org, blob, db_now(dsns)))
    assert chain.ok
    assert report.problems == (AnchorProblem("mismatch", anchor.object_key, 6),)
    assert main(["verify", "--org", org, "--anchors"]) == 1
    assert f"anchor mismatch: {anchor.object_key} seq 6" in capsys.readouterr().out
    # Rewriting the anchor row to match does not help: the object is the witness.
    new_hash = rows(dsns, org, "select hash from ssc.audit_event where seq = 6")[0][0]
    as_superuser(
        dsns,
        ("update ssc.audit_anchor set hash = %s where org_id = %s", (new_hash, org)),
    )
    _, report = asyncio.run(check(dsns.app, org, blob, db_now(dsns)))
    assert {p.cause for p in report.problems} == {"differs", "mismatch"}


def test_a_lost_tail_leaves_the_anchor_ahead(dsns: Dsns, tmp_path: Path) -> None:
    org = org_with_events(dsns)
    blob = blob_store(tmp_path)
    anchor = asyncio.run(anchor_of(dsns.app, org, blob))
    hash5 = rows(dsns, org, "select hash from ssc.audit_event where seq = 5")[0][0]
    as_superuser(
        dsns,
        ("delete from ssc.audit_event where org_id = %s and seq = 6", (org,)),
        ("update ssc.audit_head set seq = 5, hash = %s where org_id = %s", (hash5, org)),
    )
    chain, report = asyncio.run(check(dsns.app, org, blob, db_now(dsns)))
    assert chain.ok and chain.head_seq == 5
    assert report.problems == (AnchorProblem("ahead", anchor.object_key, 6),)


def test_missing_and_malformed_objects_are_reported(dsns: Dsns, tmp_path: Path) -> None:
    org = org_with_events(dsns)
    blob = blob_store(tmp_path)
    anchor = asyncio.run(anchor_of(dsns.app, org, blob))
    ghost = daily_key(org, LATER)
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, org)
        conn.execute(
            "insert into ssc.audit_anchor (org_id, anchored_at, seq, hash, object_key, reason) "
            "values (%s, now(), %s, %s, %s, 'daily')",
            (org, anchor.seq, anchor.hash, ghost),
        )
    junk = f"audit-anchors/{org}/notes.txt"
    asyncio.run(blob.put(junk, b"not an anchor"))
    _, report = asyncio.run(check(dsns.app, org, blob, db_now(dsns)))
    assert set(report.problems) == {
        AnchorProblem("missing", ghost, 6),
        AnchorProblem("malformed", junk, None),
    }


def test_anchors_older_than_36_hours_are_stale(dsns: Dsns, tmp_path: Path) -> None:
    org = org_with_events(dsns, 0)
    blob = blob_store(tmp_path)
    created = rows(dsns, org, "select created_at from ssc.org")[0][0]
    _, fresh = asyncio.run(check(dsns.app, org, blob, created + timedelta(hours=35)))
    assert fresh.ok and fresh.newest is None
    _, never = asyncio.run(check(dsns.app, org, blob, created + timedelta(hours=37)))
    assert never.problems == (AnchorProblem("stale", None, None),)
    anchor = asyncio.run(anchor_of(dsns.app, org, blob))
    _, fresh = asyncio.run(check(dsns.app, org, blob, anchor.anchored_at + timedelta(hours=35)))
    assert fresh.ok
    _, old = asyncio.run(check(dsns.app, org, blob, anchor.anchored_at + timedelta(hours=37)))
    assert old.problems == (AnchorProblem("stale", anchor.object_key, anchor.seq),)


# ── the restore procedure ────────────────────────────────────────────────────


def restore(dsns: Dsns, org: str, to_seq: int) -> None:
    """What a point-in-time restore does to one org: later events and anchor rows are gone."""
    head_hash = rows(dsns, org, "select hash from ssc.audit_event where seq = %s", to_seq)[0][0]
    as_superuser(
        dsns,
        ("delete from ssc.audit_event where org_id = %s and seq > %s", (org, to_seq)),
        ("delete from ssc.audit_anchor where org_id = %s and seq > %s", (org, to_seq)),
        (
            "update ssc.audit_head set seq = %s, hash = %s where org_id = %s",
            (to_seq, head_hash, org),
        ),
    )


def test_reanchor_after_a_restore_supersedes_the_lost_anchors(
    dsns: Dsns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    org = org_with_events(dsns)
    blob = blob_store(tmp_path)
    kept = asyncio.run(anchor_of(dsns.app, org, blob, DAY))
    restored_to = db_now(dsns)
    asyncio.run(append(dsns.app, org, 3))
    lost = asyncio.run(anchor_of(dsns.app, org, blob, LATER))
    assert lost.seq == 9
    restore(dsns, org, 6)
    now = db_now(dsns)
    _, before = asyncio.run(check(dsns.app, org, blob, now))
    assert before.problems == (AnchorProblem("ahead", lost.object_key, 9),)

    async def reanchored(to: datetime) -> Anchor:
        engine = make_engine(dsns.app)
        try:
            return await reanchor(engine, org, blob, restored_to=to, ref="INC-42", now=now)
        finally:
            await engine.dispose()

    # A restore point after the lost anchor is wrong: that anchor's events should be here.
    with pytest.raises(ReanchorRefusedError) as refusal:
        asyncio.run(reanchored(now))
    assert refusal.value.anchors.problems == (AnchorProblem("ahead", lost.object_key, 9),)
    anchor = asyncio.run(reanchored(restored_to))
    assert (anchor.reason, anchor.restored_to, anchor.seq) == ("restore", restored_to, 6)
    assert anchor.object_key.startswith(f"audit-anchors/{org}/restore-")
    ((action, target, after),) = rows(
        dsns, org, "select action, target_id, after from ssc.audit_event where seq = 7"
    )
    assert (action, target) == (AuditAction.AUDIT_REANCHORED, anchor.object_key)
    assert after == {
        "restored_to": restored_to.astimezone(UTC).isoformat(),
        "prior_anchor_seq": 9,
        "head_seq": 6,
        "ref": "INC-42",
    }
    # Life goes on past seq 9 with new events; the lost anchor stays superseded.
    asyncio.run(append(dsns.app, org, 3))
    chain, after_report = asyncio.run(check(dsns.app, org, blob, db_now(dsns)))
    assert chain.ok and after_report.ok
    assert (after_report.checked, after_report.superseded) == (2, 1)
    assert after_report.newest == anchor
    # The lost day's job, run again, finds its object covered by the restore anchor.
    assert asyncio.run(anchor_of(dsns.app, org, blob, LATER)) == anchor
    assert rows(dsns, org, "select object_key from ssc.audit_anchor order by anchored_at") == [
        (kept.object_key,),
        (anchor.object_key,),
    ]
    fs_env(monkeypatch, tmp_path, dsns)
    assert main(["verify", "--org", org, "--anchors"]) == 0
    assert "2 checked, 1 superseded" in capsys.readouterr().out


def test_reanchor_refuses_a_tampered_chain_and_writes_nothing(
    dsns: Dsns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    org = org_with_events(dsns)
    blob = blob_store(tmp_path)
    anchor = asyncio.run(anchor_of(dsns.app, org, blob))
    restored_to = db_now(dsns)
    recompute_from(dsns, org, 2)
    fs_env(monkeypatch, tmp_path, dsns)
    at = restored_to.isoformat()
    assert main(["reanchor", "--org", org, "--restored-to", at, "--ref", "INC-7"]) == 1
    out = capsys.readouterr().out
    assert f"anchor mismatch: {anchor.object_key} seq 6" in out
    assert out.endswith("refused: nothing written\n")
    assert rows(dsns, org, "select reason from ssc.audit_anchor") == [("daily",)]
    assert rows(dsns, org, "select max(seq) from ssc.audit_event") == [(6,)]
    assert asyncio.run(keys(blob, org)) == [anchor.object_key]
    with pytest.raises(SystemExit) as e:
        main(["reanchor", "--org", org, "--restored-to", "2026-09-01T00:00:00", "--ref", "x"])
    assert e.value.code == 2  # a restore point without an offset is a usage error


# ── the CLI ──────────────────────────────────────────────────────────────────


def fs_env(monkeypatch: pytest.MonkeyPatch, root: Path, dsns: Dsns) -> None:
    monkeypatch.setenv("SSC_DATABASE_DSN", dsns.app)
    monkeypatch.setenv("SSC_ENV", "test")
    monkeypatch.setenv("SSC_BLOB_BACKEND", "fs")
    monkeypatch.setenv("SSC_BLOB_ROOT", str(root))
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv("SSC_BLOB_SIGNING_KEYS", json.dumps({"k1": key}))
    monkeypatch.setenv("SSC_BLOB_SIGNING_KID", "k1")


def test_cli_anchors_now_and_needs_a_blob_store(
    dsns: Dsns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    org = org_with_events(dsns, 1)
    monkeypatch.setenv("SSC_DATABASE_DSN", dsns.app)
    monkeypatch.delenv("SSC_BLOB_BACKEND", raising=False)
    for argv in (["anchor", "--org", org], ["verify", "--org", org, "--anchors"]):
        with pytest.raises(SystemExit) as e:
            main(argv)
        assert e.value.code == 2
        assert "blob store" in capsys.readouterr().err
    fs_env(monkeypatch, tmp_path, dsns)
    monkeypatch.setenv("SSC_ENV", "prod")
    with pytest.raises(SystemExit) as e:
        main(["anchor", "--org", org])
    assert e.value.code == 2  # the filesystem store is for dev and test only
    monkeypatch.setenv("SSC_ENV", "test")
    assert main(["anchor", "--org", org]) == 0
    today = db_now(dsns).astimezone(UTC).date()
    assert capsys.readouterr().out == f"anchored: seq 2 at {daily_key(org, today)}\n"


# ── the job ──────────────────────────────────────────────────────────────────


def fresh_db(dsns: Dsns, revision: str = "head") -> Dsns:
    name = f"a{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")

    def at(dsn: str) -> str:
        return make_url(dsn).set(database=name).render_as_string(hide_password=False)

    db = Dsns(at(dsns.superuser), at(dsns.migrate), at(dsns.app))
    upgrade(db.migrate, revision)
    return db


async def run_tick(db: Dsns, ports: Ports, at: datetime) -> None:
    """One ``anchor_tick`` at ``at``, then every job it defers, in one worker pass."""
    app = App(connector=PsycopgConnector(conninfo=queue_conninfo(db.app)))
    app.add_tasks_from(audit_jobs.blueprint(), namespace="audit")
    async with app.open_async():
        await app.configure_task("audit:anchor_tick").defer_async(timestamp=int(at.timestamp()))
        await app.run_worker_async(
            additional_context={PORTS_KEY: ports}, wait=False, install_signal_handlers=False
        )


def anchor_jobs(db: Dsns) -> list[tuple[str, str]]:
    with psycopg.connect(db.superuser) as conn:
        return [
            (str(org), str(status))
            for org, status in conn.execute(
                "select args->>'org_id', status from procrastinate.procrastinate_jobs "
                "where task_name = %s order by 1",
                (audit_jobs.ANCHOR_TASK,),
            )
        ]


async def test_the_tick_anchors_every_org_without_the_days_anchor(
    dsns: Dsns, tmp_path: Path
) -> None:
    db = await asyncio.to_thread(fresh_db, dsns)
    a, b, c = [(await asyncio.to_thread(make_org, db.app, n)).org_id for n in ("A", "B", "C")]
    blob = blob_store(tmp_path)
    now = datetime.now(UTC)
    today = now.date()
    done = await anchor_of(db.app, a, blob, today)
    engine = make_engine(db.app)
    try:
        await run_tick(db, Ports(engine=engine), now)  # no blob store: nothing deferred
        assert anchor_jobs(db) == []
        await run_tick(db, Ports(engine=engine, blob_store=blob), now)
    finally:
        await engine.dispose()
    assert anchor_jobs(db) == sorted([(b, "succeeded"), (c, "succeeded")])
    for org in (a, b, c):
        stored = await read(blob, daily_key(org, today))
        assert parse_anchor(daily_key(org, today), stored, org_id=org).seq == 1
    assert await read(blob, done.object_key) == done.document()


def test_the_anchor_job_retries_forever_backing_off_to_an_hour() -> None:
    backoff = audit_jobs.CappedBackoff()
    waits = []
    for attempts in (0, 1, 2, 6, 7, 50, 10_000):
        job = cast(Any, SimpleNamespace(attempts=attempts))
        decision = backoff.get_retry_decision(exception=RuntimeError("x"), job=job)
        assert decision.retry_at is not None
        waits.append(round((decision.retry_at - datetime.now(UTC)).total_seconds() / 30) * 30)
    assert waits == [30, 60, 120, 1920, 3600, 3600, 3600]


def test_build_app_registers_the_audit_tasks_hourly() -> None:
    app = build_app("postgresql://ssc_app@localhost/ssc")
    assert {"audit:anchor_tick", audit_jobs.ANCHOR_TASK} <= set(app.tasks)
    ((periodic,),) = [
        [
            p
            for key, p in app.periodic_registry.periodic_tasks.items()
            if key[0] == "audit:anchor_tick"
        ]
    ]
    start = datetime(2026, 9, 1, 23, 30, tzinfo=UTC).timestamp()
    first = periodic.croniter.get_next(float, start_time=start)
    assert datetime.fromtimestamp(first, UTC) == datetime(2026, 9, 2, 0, 5, tzinfo=UTC)


# ── the table ────────────────────────────────────────────────────────────────


def test_anchor_rows_are_per_org_and_append_only(dsns: Dsns, tmp_path: Path) -> None:
    a, b = org_with_events(dsns, 1), org_with_events(dsns, 1)
    blob = blob_store(tmp_path)
    asyncio.run(anchor_of(dsns.app, a, blob))
    assert rows(dsns, b, "select * from ssc.audit_anchor") == []
    insert = (
        "insert into ssc.audit_anchor (org_id, anchored_at, seq, hash, object_key, reason) "
        "values (%s, now(), 0, %s, 'audit-anchors/x', 'daily')"
    )
    assert refused(dsns.app, b, insert, (a, bytes(32))) == INSUFFICIENT_PRIVILEGE
    update = "update ssc.audit_anchor set seq = 0 where org_id = %s"
    delete = "delete from ssc.audit_anchor where org_id = %s"
    assert refused(dsns.app, a, update, (a,)) == INSUFFICIENT_PRIVILEGE
    assert refused(dsns.app, a, delete, (a,)) == INSUFFICIENT_PRIVILEGE
    assert refused(dsns.app, a, "truncate ssc.audit_anchor") == INSUFFICIENT_PRIVILEGE
    assert refused(dsns.migrate, a, update, (a,)) == SqlState.AUDIT_IMMUTABLE
    assert refused(dsns.migrate, a, delete, (a,)) == SqlState.AUDIT_IMMUTABLE
    assert refused(dsns.migrate, a, "truncate ssc.audit_anchor") == SqlState.TRUNCATE_REFUSED
    restore_without_time = (
        "insert into ssc.audit_anchor (org_id, anchored_at, seq, hash, object_key, reason) "
        "values (%s, now(), 0, %s, 'audit-anchors/y', 'restore')"
    )
    assert refused(dsns.app, a, restore_without_time, (a, bytes(32))) == "23514"


# ── cell labels (revision 0011) ──────────────────────────────────────────────

GENERATED = re.compile(r"^[bcdfghjkmnpqrstv]{12}$")
HOST_LABEL = re.compile(r"^[a-z][a-z0-9]{7,15}$")  # snapshot_ack's CHECK


def labels(dsn: str) -> dict[str, str | None]:
    with psycopg.connect(dsn) as conn:
        return dict(conn.execute("select id, cell_label from ssc.org").fetchall())


def test_every_org_gets_its_own_cell_label_at_creation(dsns: Dsns) -> None:
    created = [make_org(dsns.app, f"Labelled {n}") for n in range(5)]
    stored = labels(dsns.superuser)
    for org in created:
        assert stored[org.org_id] == org.cell_label
        assert GENERATED.match(org.cell_label) and HOST_LABEL.match(org.cell_label)
    assert len({org.cell_label for org in created}) == len(created)


def column_state(dsn: str) -> tuple[bool, bool, bool, bool, bool]:
    """(label NOT NULL, label has a default, FORCE RLS on org, content_digest, audit_anchor)."""
    (row,) = run_sql(
        dsn,
        "select a.attnotnull, a.atthasdef, c.relforcerowsecurity, "
        "exists (select from information_schema.columns where table_schema = 'ssc' "
        "  and table_name = 'access_snapshot' and column_name = 'content_digest'), "
        "to_regclass('ssc.audit_anchor') is not null "
        "from pg_attribute a join pg_class c on c.oid = a.attrelid "
        "where a.attrelid = 'ssc.org'::regclass and a.attname = 'cell_label'",
    )
    return cast(tuple[bool, bool, bool, bool, bool], row)


def run_sql(dsn: str, sql: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(sql).fetchall()


def insert_org(dsn: str, name: str, label: str | None = None) -> str:
    org = new_id("org")
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        if label is None:
            conn.execute("insert into ssc.org (id, name) values (%s, %s)", (org, name))
        else:
            conn.execute(
                "insert into ssc.org (id, name, cell_label) values (%s, %s, %s)",
                (org, name, label),
            )
    return org


def test_0011_backfills_cell_labels_and_round_trips(dsns: Dsns) -> None:
    scripts = ScriptDirectory.from_config(alembic_config(dsns.migrate))
    revision = scripts.get_revision("0011_audit_anchor")
    assert revision is not None and isinstance(revision.down_revision, str)
    before = revision.down_revision
    db = fresh_db(dsns, before)
    assert column_state(db.migrate) == (False, False, True, False, False)
    old = insert_org(db.app, "Before 0011")
    kept = insert_org(db.app, "Already labelled", "cellwxyz")
    assert labels(db.superuser) == {old: None, kept: "cellwxyz"}

    upgrade(db.migrate)
    assert column_state(db.migrate) == (True, True, True, True, True)  # FORCE is back on
    after = labels(db.superuser)
    assert after[kept] == "cellwxyz"
    assert GENERATED.match(after[old] or "")
    new = insert_org(db.app, "After 0011")  # the default serves writers that omit the column
    assert GENERATED.match(labels(db.superuser)[new] or "")

    downgrade(db.migrate, before)
    assert column_state(db.migrate) == (False, False, True, False, False)
    assert labels(db.superuser)[old] == after[old]  # labels are kept
    upgrade(db.migrate)
    assert column_state(db.migrate) == (True, True, True, True, True)
