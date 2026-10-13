"""GA-12: deleting an app, against the fake runtime driver, the fake cell ports (app databases,
files and secrets), a filesystem bundle store and postgres:18. The bench and its helpers are the
kill switch's (``test_lifecycle``): a removal is that run in mode ``delete``.

Ticket "done when" checks:
  * everything of the app goes, in order -> test_a_removal_runs_in_order_and_removes_everything
  * audit rows for who, when, each step  -> the same, and test_the_audit_chain_stays_valid
  * org admin only, never an agent       -> test_who_may_delete_and_when
  * confirm by the exact slug            -> test_who_may_delete_and_when
  * refused while work is in flight      -> test_who_may_delete_and_when
  * a failed step stops the run; asking  -> test_a_failed_step_stops_the_run_until_asked_again
    again finishes it
  * already gone is success              -> test_a_removal_of_an_app_with_nothing_completes
  * the database place is freed          -> test_only_this_app_is_touched_and_its_place_is_freed
  * nothing of another app is touched    -> the same
  * tombstone: gone everywhere, slug     -> test_a_deleted_app_is_gone_and_its_slug_is_free
    free, history kept
  * timers, approvals, connections, repo -> test_schedules_links_and_open_requests_go_with_the_app
  * the control plane's calls to a cell  -> test_the_agent_clients_ask_the_cell_and_check_the_answer
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx2
import psycopg
import pytest
import test_lifecycle
from httpx import Response
from ssc_testkit import SigningKey, assert_problem, auth, mint, new_key
from test_lifecycle import (
    FAST,
    Bench,
    app_status,
    audit_of,
    bring_up,
    drain,
    execute,
    fs_blob,
    get,
    kill_jobs,
    post,
    pull,
    reconcile,
    rows_of,
)

from ssc_contracts import app_database, egress
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.mcp.tools import TOOLS
from ssc_control.audit.verify import verify
from ssc_control.db import bind_org_sync, bound_org
from ssc_control.deploy.build_driver import FakeBuildDriver
from ssc_control.deploy.bundles import bundle_key
from ssc_control.lifecycle import kill_switch
from ssc_control.runtime.app_databases import (
    AppDatabaseError,
    CellAppDatabases,
    FakeAppDatabases,
)
from ssc_control.runtime.cell_removal import AgentCellRemoval, CellRemovalError, FakeCellRemoval
from ssc_control.runtime.cells import STATIC_LABEL, OrgCell, StaticCells
from ssc_control.runtime.driver import service_name
from ssc_control.snapshot.compiler import compile_document
from ssc_control.timers.service import Timers
from ssc_control.worker_ports import Ports
from ssc_shared.blobstore_fs import FsBlobStore
from ssc_shared.hosts import host_label
from ssc_shared.runtime import ORG_HEADER, secret_id

world = test_lifecycle.world
tokens = test_lifecycle.tokens
b = test_lifecycle.b

STEPS = list(kill_switch.DELETE_STEPS)
PLATFORM_SECRETS = (*app_database.SECRETS, *egress.SECRETS)


@dataclass
class Databases(FakeAppDatabases):
    """The fake, refusing to drop the services in ``broken``."""

    broken: set[str] = field(default_factory=set[str])

    async def drop(self, service: str) -> bool:
        if service in self.broken:
            self.calls.append(("drop", service))
            raise AppDatabaseError("DATABASE_UNAVAILABLE", "the instance did not answer")
        return await super().drop(service)


@dataclass
class Rig:
    """The worker's ports with a cell that can delete: what a removal's steps call."""

    ports: Ports
    databases: Databases
    removal: FakeCellRemoval
    blob: FsBlobStore


def rig(b: Bench, tmp_path: Path, **ports: Any) -> Rig:
    databases, removal, blob = Databases(), FakeCellRemoval(), fs_blob(tmp_path / "blobs")
    cell = OrgCell(
        label=STATIC_LABEL,
        runtime=b.driver,
        build=FakeBuildDriver(),
        app_databases=databases,
        removal=removal,
    )
    made = replace(b.ports, cells=StaticCells(cell), blob_store=blob, **ports)
    return Rig(made, databases, removal, blob)


def delete(
    b: Bench, confirm: str = "ledger", token: str | None = None, app: str | None = None
) -> Response:
    return post(b, f"/v1/apps/{app or b.w.app}/delete", {"confirm": confirm}, token)


def deleting(b: Bench) -> str:
    r = delete(b)
    assert r.status_code == 202, r.text
    return str(r.json()["run_id"])


def ask(b: Bench, env: str) -> Response:
    """Ask for an approval about ``env``, as a builder or an agent would."""
    body = {"environment_id": env, "kind": "connect_data_source", "subject_key": "hr"}
    return post(b, "/v1/approvals", {**body, "payload": {}})


def run_of(b: Bench, run_id: str) -> dict[str, Any]:
    r = get(b, f"/v1/apps/{b.w.app}/delete/{run_id}")
    assert r.status_code == 200, r.text
    return cast("dict[str, Any]", r.json())


def count(b: Bench, table: str, where: str = "true", *args: object) -> int:
    (row,) = rows_of(b.dsn, b.w.org, f"select count(*) as n from ssc.{table} where {where}", *args)
    return int(row["n"])


def give_database(b: Bench, env: str) -> None:
    """A place on the instance, as a deployment records it, with the secrets it mounts."""
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.app_database (org_id, environment_id, host, port, connection_limit) "
        "values (%s, %s, '10.0.0.5', 5432, 20)",
        b.w.org,
        env,
    )
    for name in app_database.SECRETS:
        give_secret(b, env, name)


def give_secret(b: Bench, env: str, name: str) -> None:
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.secret_ref (id, org_id, environment_id, name, secret_version) "
        "values (%s, %s, %s, %s, '1')",
        new_id("sec"),
        b.w.org,
        env,
        name,
    )


async def give_bundle(b: Bench, blob: FsBlobStore, label: str, app: str | None = None) -> str:
    """A stored bundle of the app, row and object; the object's key."""
    digest = "sha256:" + hashlib.sha256(label.encode()).hexdigest()
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.bundle (id, org_id, app_id, digest, size_bytes, actor_kind, actor_id, "
        "state, manifest, manifest_digest, file_count, stored_at) "
        "values (%s, %s, %s, %s, 5, 'user', %s, 'stored', '{}'::jsonb, %s, 0, now())",
        new_id("bdl"),
        b.w.org,
        app or b.w.app,
        digest,
        b.w.builder,
        digest,
    )
    key = bundle_key(b.w.org, app or b.w.app, digest)
    await blob.put(key, b"bytes")
    return key


def other_app(b: Bench, slug: str = "payroll") -> tuple[str, str, str]:
    """A second app of the org with a builder grant on each environment: its id and theirs."""
    app, prod, preview = new_id("app"), new_id("env"), new_id("env")
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        conn.execute(
            "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, %s, %s)",
            (app, b.w.org, slug, b.w.admin),
        )
        for env, name in ((prod, "prod"), (preview, "preview")):
            conn.execute(
                "insert into ssc.environment (id, org_id, app_id, name) values (%s, %s, %s, %s)",
                (env, b.w.org, app, name),
            )
            conn.execute(
                "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, "
                "user_id, granted_by_user_id) values (%s, %s, %s, 'builder', 'user', %s, %s)",
                (new_id("gnt"), b.w.org, env, b.w.builder, b.w.admin),
            )
    return app, prod, preview


# ── the run ──────────────────────────────────────────────────────────────────


async def test_a_removal_runs_in_order_and_removes_everything(b: Bench, tmp_path: Path) -> None:
    await bring_up(b)
    r = rig(b, tmp_path)
    give_database(b, b.w.prod)
    give_secret(b, b.w.prod, "STRIPE_KEY")
    give_secret(b, b.w.preview, "STRIPE_KEY")
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.egress_credential (org_id, environment_id, credential_id, sha1, "
        "secret_version) values (%s, %s, 'abcdefabcdef', %s, '1')",
        b.w.org,
        b.w.prod,
        "A" * 27 + "=",
    )
    prod, preview = service_name(b.w.prod), service_name(b.w.preview)
    r.removal.files[prod] = 3
    key = await give_bundle(b, r.blob, "one")

    asked = delete(b)
    assert asked.status_code == 202, asked.text
    run_id = asked.json()["run_id"]
    assert asked.json() == {"run_id": run_id, "state": "running"}
    assert asked.headers["Location"] == f"/v1/apps/{b.w.app}/delete/{run_id}"
    # Committed and answered with the app denied and nothing deleted yet.
    assert app_status(b) == "deleting"
    assert run_of(b, run_id)["steps"] == [] and b.driver.calls == [] and r.removal.calls == []
    (first,) = audit_of(b, b.w.app)
    assert (first["action"], first["actor_id"]) == ("app.deleted", b.w.admin)
    assert (first["before"], first["after"]) == ({"status": "active"}, {"status": "deleting"})

    ran = await drain(b, r.ports)
    assert ran[-1].outcome == "completed"
    # The cell steps each took both environments, prod first, under that environment's lock.
    assert [x.lock for x in ran[1:]] == [f"env:{b.w.prod}", f"env:{b.w.preview}"] * 5
    got = run_of(b, run_id)
    assert (got["state"], got["app_id"]) == ("completed", b.w.app)
    assert [s["name"] for s in got["steps"]] == STEPS
    assert {s["state"] for s in got["steps"]} == {"done"}
    assert got["total_ms"] is not None and got["finished_at"] is not None
    removed = {s["name"]: s["removed"] for s in got["steps"]}
    assert removed == {
        "gateway_deny": None,
        "datagw_suspend": None,
        "egress_remove": None,
        "scale_to_zero": None,
        "remove_timers": 0,
        "drop_databases": 1,
        "delete_files": 3,
        "delete_secrets": 2,
        "delete_services": 2,
        "delete_bundles": 1,
        "remove_records": 2,
    }

    # On the cell: stopped first, then database, files, secrets, and the service last.
    assert b.driver.services == {}
    scaled = [e for e in b.events if e[0] == "scale_to_zero"]
    assert scaled == [("scale_to_zero", prod), ("scale_to_zero", preview)]
    assert r.databases.calls == [("drop", prod), ("drop", preview)]
    assert [c for c in r.removal.calls if c[0] == "drop_files"] == [
        ("drop_files", prod),
        ("drop_files", preview),
    ]
    secrets_removed = [name for kind, name in r.removal.calls if kind == "remove_secret"]
    assert sorted(secrets_removed) == sorted(
        secret_id(service, name)
        for service in (prod, preview)
        for name in dict.fromkeys(("STRIPE_KEY", *PLATFORM_SECRETS))
    )
    removes = [c for c in b.driver.calls if c[0] == "remove"]
    assert removes == [("remove", prod), ("remove", preview)]
    assert [info.key async for info in r.blob.list("bundles/")] == [] and key

    # In the database: what tied the app to anything is gone, the app and its history stay.
    for table in ("app_database", "secret_ref", "egress_credential", "app_grant"):
        assert count(b, table) == 0, table
    assert app_status(b) == "deleted"
    assert count(b, "environment", "app_id = %s", b.w.app) == 2
    assert count(b, "release", "app_id = %s", b.w.app) == 2
    assert count(b, "deployment", "app_id = %s", b.w.app) == 2
    assert rows_of(b.dsn, b.w.org, "select state from ssc.bundle") == [{"state": "pending"}]

    # The audit log: the request, each step with its outcome, each thing removed, the end.
    steps = audit_of(b, run_id)
    assert [a["action"] for a in steps] == ["kill_switch.step"] * len(STEPS)
    assert [a["after"]["step"] for a in steps] == STEPS
    assert {(a["after"]["mode"], a["after"]["state"], a["actor_id"]) for a in steps} == {
        ("delete", "done", b.w.admin)
    }
    assert steps[5]["after"]["removed"] == 1 and "removed" not in steps[0]["after"]
    first, last = audit_of(b, b.w.app)
    assert (last["action"], last["actor_id"]) == ("app.deleted", b.w.admin)
    assert (last["before"], last["after"]) == ({"status": "deleting"}, {"status": "deleted"})
    actions = rows_of(
        b.dsn,
        b.w.org,
        "select action, count(*) as n from ssc.audit_event "
        "where action in ('secret.removed', 'grant.removed') group by action order by action",
    )
    assert actions == [
        {"action": "grant.removed", "n": 2},
        {"action": "secret.removed", "n": 2 + len(app_database.SECRETS)},
    ]

    # Nothing brings it back: the reconciler leaves a deleted app's environments alone.
    await reconcile(b)
    assert b.driver.services == {} and kill_jobs(b.dsn, b.w.app, "todo") == []


async def test_a_removal_of_an_app_with_nothing_completes(b: Bench, tmp_path: Path) -> None:
    """Never deployed: no service, database, file, secret or bundle. Every step finds nothing."""
    r = rig(b, tmp_path)
    run_id = deleting(b)
    assert (await drain(b, r.ports))[-1].outcome == "completed"
    got = run_of(b, run_id)
    assert [(s["name"], s["state"]) for s in got["steps"]] == [(n, "done") for n in STEPS]
    assert [s["removed"] for s in got["steps"][4:]] == [0, 0, 0, 0, 0, 0, 2]
    assert app_status(b) == "deleted"
    # The cell was still asked for each environment: a failed deployment can leave things there.
    assert len(r.databases.calls) == 2
    assert [c[0] for c in b.driver.calls] == ["scale_to_zero", "observe"] * 2 + [
        "observe",
        "remove",
    ] * 2


async def test_who_may_delete_and_when(b: Bench, signing_key: SigningKey) -> None:
    await bring_up(b)
    assert_problem(delete(b, token=b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(delete(b, token=b.t.builder), ErrorCode.FORBIDDEN)
    agent = mint(signing_key, org=b.w.org, sub=b.w.admin, jti=f"cred_{new_key()[:16]}", agent=True)
    assert_problem(delete(b, token=agent), ErrorCode.AGENT_SESSION_REFUSED)
    for wrong in ("", "Ledger", "ledger ", b.w.app, "payroll"):
        assert_problem(delete(b, wrong), ErrorCode.CONFIRMATION_MISMATCH)
    assert_problem(delete(b, app=new_id("app")), ErrorCode.NOT_FOUND)
    assert not any("delete" in tool or "remove" in tool for tool in TOOLS)

    # A deploy, promote or rollback is a deployment; while one is under way, or a build, no.
    (release,) = rows_of(b.dsn, b.w.org, "select id from ssc.release where number = 1")
    dep = new_id("dep")
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.deployment (id, org_id, app_id, environment_id, release_id, kind, "
        "state, config_version, grants_version, actor_kind, actor_id) "
        "values (%s, %s, %s, %s, %s, 'rollback', 'running', 1, 1, 'user', %s)",
        dep,
        b.w.org,
        b.w.app,
        b.w.preview,
        release["id"],
        b.w.builder,
    )
    assert_problem(delete(b), ErrorCode.APP_OPERATION_IN_FLIGHT)
    execute(
        b.dsn,
        b.w.org,
        "update ssc.deployment set state = 'failed', failure_code = 'HEALTH_CHECK_FAILED', "
        "finished_at = now() where id = %s",
        dep,
    )

    # Nor while its kill switch is being pulled.
    assert pull(b).status_code == 202
    assert_problem(delete(b), ErrorCode.KILL_SWITCH_IN_FLIGHT)
    assert (await drain(b))[-1].outcome == "completed"

    # Refused every time, so nothing changed and nothing is recorded as a removal.
    assert app_status(b) == "disabled"
    assert [a["action"] for a in audit_of(b, b.w.app)] == ["app.disabled"]
    # A disabled app can be deleted; a second ask while the first runs is refused.
    run_id = deleting(b)
    assert_problem(delete(b), ErrorCode.KILL_SWITCH_IN_FLIGHT)
    assert_problem(get(b, f"/v1/apps/{b.w.app}/delete/{run_id}", b.t.builder), ErrorCode.FORBIDDEN)
    # The removal is not a pull of the switch, and the other way round.
    assert_problem(get(b, f"/v1/apps/{b.w.app}/kill-switch/{run_id}"), ErrorCode.NOT_FOUND)


async def test_a_failed_step_stops_the_run_until_asked_again(b: Bench, tmp_path: Path) -> None:
    await bring_up(b)
    r = rig(b, tmp_path)
    give_database(b, b.w.prod)
    give_database(b, b.w.preview)
    prod, preview = service_name(b.w.prod), service_name(b.w.preview)
    r.removal.files[preview] = 4
    r.databases.broken.add(preview)

    first = deleting(b)
    ran = await drain(b, r.ports, timings=replace(FAST, max_attempts=2))
    assert ran[-1].outcome == "failed"
    got = run_of(b, first)
    assert got["state"] == "failed" and got["finished_at"] is not None
    # It stopped at the step that failed: nothing after it was begun.
    assert [s["name"] for s in got["steps"]] == STEPS[:6]
    failed = got["steps"][-1]
    assert (failed["state"], failed["error"], failed["attempts"]) == (
        "failed",
        "DATABASE_UNAVAILABLE",
        2,
    )
    assert failed["removed"] == 1  # prod's went
    assert r.databases.calls == [("drop", prod), ("drop", preview), ("drop", preview)]
    assert r.removal.calls == [] and not any(c[0] == "remove" for c in b.driver.calls)
    assert count(b, "app_database") == 1 and count(b, "app_grant") == 2
    assert audit_of(b, first)[-1]["after"]["error"] == "DATABASE_UNAVAILABLE"

    # The app is still there for its admins, denied, and takes no change.
    assert app_status(b) == "deleting"
    shown = get(b, f"/v1/apps/{b.w.app}")
    assert shown.status_code == 200 and shown.json()["status"] == "deleting"
    (item,) = get(b, "/v1/inventory").json()["items"]
    assert item["status"] == "deleting"
    env = f"/v1/apps/{b.w.app}/environments/{b.w.preview}"
    for refused in (
        pull(b),
        post(b, f"/v1/apps/{b.w.app}/enable", {}),
        post(b, f"/v1/apps/{b.w.app}/promote", {}),
        post(b, f"{env}/deployments", {"release_id": new_id("rel")}),
        post(b, f"{env}/database/rotate", {}),
        b.client.put(f"{env}/grants", json={"grants": []}, headers=auth(b.t.admin)),
        b.client.put(f"/v1/apps/{b.w.app}/owner", json={}, headers=auth(b.t.admin)),
    ):
        assert_problem(refused, ErrorCode.APP_DELETING)
    assert_problem(ask(b, b.w.preview), ErrorCode.APP_DELETING)
    await reconcile(b)
    assert b.driver.services[prod].stopped and b.driver.services[preview].stopped

    # Asking again starts a new run, which finds what is gone and finishes the rest.
    r.databases.broken.clear()
    second = deleting(b)
    assert second != first
    assert (await drain(b, r.ports))[-1].outcome == "completed"
    again = run_of(b, second)
    assert [(s["name"], s["state"]) for s in again["steps"]] == [(n, "done") for n in STEPS]
    removed = {s["name"]: s["removed"] for s in again["steps"]}
    assert (removed["drop_databases"], removed["delete_files"]) == (1, 4)
    assert (removed["delete_services"], removed["remove_records"]) == (2, 2)
    assert app_status(b) == "deleted" and count(b, "app_database") == 0
    assert run_of(b, first)["state"] == "failed"
    assert [a["after"]["status"] for a in audit_of(b, b.w.app)] == [
        "deleting",
        "deleting",
        "deleted",
    ]


async def test_a_cell_that_cannot_delete_fails_the_step(b: Bench) -> None:
    """The bench's cell has a runtime and nothing else: a recorded database it cannot drop is
    a failure, not a place forgotten; so are files nobody can delete."""
    await bring_up(b)
    give_database(b, b.w.prod)
    first = deleting(b)
    assert (await drain(b, timings=replace(FAST, max_attempts=1)))[-1].outcome == "failed"
    last = run_of(b, first)["steps"][-1]
    assert (last["name"], last["error"]) == ("drop_databases", "DATABASE_UNAVAILABLE")
    assert count(b, "app_database") == 1
    execute(b.dsn, b.w.org, "delete from ssc.app_database")
    second = deleting(b)
    assert (await drain(b, timings=replace(FAST, max_attempts=1)))[-1].outcome == "failed"
    last = run_of(b, second)["steps"][-1]
    assert (last["name"], last["error"]) == ("delete_files", "REMOVAL_UNAVAILABLE")
    assert app_status(b) == "deleting" and len(b.driver.services) == 2


# ── only this app ────────────────────────────────────────────────────────────


async def test_only_this_app_is_touched_and_its_place_is_freed(b: Bench, tmp_path: Path) -> None:
    await bring_up(b)
    r = rig(b, tmp_path)
    other, other_prod, other_preview = other_app(b)
    theirs = [service_name(other_prod), service_name(other_preview)]
    for env in (b.w.prod, b.w.preview, other_prod):
        give_database(b, env)
        give_secret(b, env, "STRIPE_KEY")
    for service in (*theirs, service_name(b.w.prod)):
        await r.databases.ensure(service)
        r.removal.files[service] = 2
    r.databases.ceiling = 3
    with pytest.raises(AppDatabaseError, match="all taken"):
        await r.databases.ensure(service_name(new_id("env")))
    kept = await give_bundle(b, r.blob, "theirs", other)
    await give_bundle(b, r.blob, "ours")
    before = dict(b.driver.services)
    r.databases.calls.clear()

    def places() -> int:
        cell = get(b, "/v1/cell")
        assert cell.status_code == 200, cell.text
        return int(cell.json()["database"]["places_used"])

    assert places() == 3
    deleting(b)
    assert (await drain(b, r.ports))[-1].outcome == "completed"

    # Two places back, as the console counts them, and on the instance.
    assert places() == 1
    assert count(b, "app_database", "environment_id = %s", other_prod) == 1
    await r.databases.ensure(service_name(new_id("env")))
    # Not one call named anything of the other app.
    called = [*r.databases.calls, *r.removal.calls, *b.driver.calls]
    assert not [c for c in called if any(service in c[1] for service in theirs)]
    assert set(b.driver.services) == set(before) - {
        service_name(b.w.prod),
        service_name(b.w.preview),
    }
    assert r.removal.files == dict.fromkeys(theirs, 2)
    assert [info.key async for info in r.blob.list("bundles/")] == [kept]
    assert count(b, "secret_ref") == 1 + len(app_database.SECRETS)
    assert count(b, "app_grant") == 2
    (theirs_row,) = rows_of(b.dsn, b.w.org, "select status from ssc.app where id = %s", other)
    assert theirs_row["status"] == "active"
    shown = get(b, f"/v1/apps/{other}")
    assert shown.status_code == 200 and len(shown.json()["environments"]) == 2
    # Its confirmation is its own slug: the other app's does not delete this one.
    assert_problem(delete(b, "ledger", app=other), ErrorCode.CONFIRMATION_MISMATCH)


# ── the tombstone ────────────────────────────────────────────────────────────


async def test_a_deleted_app_is_gone_and_its_slug_is_free(b: Bench, tmp_path: Path) -> None:
    await bring_up(b)
    r = rig(b, tmp_path)

    async def document() -> dict[str, Any]:
        async with bound_org(b.ports.engine, b.w.org) as conn:
            doc = await compile_document(conn, b.w.org, version=1, compiled_at=datetime.now(UTC))
        return doc.model_dump(mode="json")

    run_id = deleting(b)
    # Being deleted: denied as a disabled app is, in the words the gateways already read.
    doc = await document()
    assert {e["status"] for e in doc["environments"].values()} == {"disabled"}
    assert doc["hosts"][host_label("ledger", "prod")] == b.w.prod
    assert_problem(post(b, "/v1/apps", {"slug": "ledger"}), ErrorCode.ALREADY_EXISTS)
    assert (await drain(b, r.ports))[-1].outcome == "completed"

    # Gone from every route that names it, and from every list.
    env = f"/v1/apps/{b.w.app}/environments/{b.w.prod}"
    for path in (
        f"/v1/apps/{b.w.app}",
        f"/v1/apps/{b.w.app}/releases",
        f"{env}/deployments",
        f"{env}/grants",
        f"{env}/secrets",
        f"{env}/database",
    ):
        assert_problem(get(b, path), ErrorCode.NOT_FOUND)
    for gone in (delete(b), pull(b), post(b, f"/v1/apps/{b.w.app}/enable", {})):
        assert_problem(gone, ErrorCode.NOT_FOUND)
    assert get(b, "/v1/apps").json()["apps"] == []
    assert get(b, "/v1/apps", b.t.builder).json()["apps"] == []
    assert get(b, "/v1/inventory").json()["items"] == []
    assert get(b, "/v1/cell").json()["environments"] == []
    assert get(b, "/v1/warm").status_code == 200
    assert_problem(ask(b, b.w.prod), ErrorCode.NOT_FOUND)
    doc = await document()
    assert doc["environments"] == {} and doc["hosts"] == {} and doc["grants"] == {}
    # Its removal, and what the audit log says of it, are still there to read.
    assert run_of(b, run_id)["state"] == "completed"
    logged = get(b, "/v1/audit").json()["events"]
    assert any(e["target"]["id"] == b.w.app for e in logged)

    # The slug is free: a new app takes it, with its own ids and the address.
    made = post(b, "/v1/apps", {"slug": "ledger"})
    assert made.status_code == 201, made.text
    fresh = made.json()
    assert fresh["id"] != b.w.app and fresh["status"] == "active"
    assert [a["id"] for a in get(b, "/v1/apps").json()["apps"]] == [fresh["id"]]
    doc = await document()
    new_prod = next(e["id"] for e in fresh["environments"] if e["name"] == "prod")
    assert doc["hosts"][host_label("ledger", "prod")] == new_prod
    assert set(doc["environments"]) == {e["id"] for e in fresh["environments"]}
    assert count(b, "app", "slug = 'ledger'") == 2
    # And the old one still cannot be asked for by the name they now share.
    assert_problem(delete(b), ErrorCode.NOT_FOUND)


async def test_the_audit_chain_stays_valid(b: Bench, tmp_path: Path) -> None:
    await bring_up(b)
    r = rig(b, tmp_path)
    give_database(b, b.w.prod)
    before = count(b, "audit_event")
    deleting(b)
    assert (await drain(b, r.ports))[-1].outcome == "completed"
    async with bound_org(b.ports.engine, b.w.org) as conn:
        report = await verify(conn, b.w.org)
    assert report.ok and report.first_broken is None
    # The request, eleven steps, the database's secrets, two grants, the end; none taken away.
    added = 1 + len(STEPS) + len(app_database.SECRETS) + 2 + 1
    assert report.checked == count(b, "audit_event") == before + added


# ── what else goes with it ───────────────────────────────────────────────────


async def test_schedules_links_and_open_requests_go_with_the_app(b: Bench, tmp_path: Path) -> None:
    await bring_up(b)
    r = rig(b, tmp_path, timers=Timers())
    other, other_prod, _ = other_app(b)
    con, pending, theirs = new_id("con"), new_id("apr"), new_id("apr")
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        for env in (b.w.prod, b.w.preview, other_prod):
            conn.execute(
                "insert into ssc.schedule (id, org_id, environment_id, name, cron, path, state, "
                "pause_reason, declared_by_user_id) "
                "values (%s, %s, %s, 'nightly', '0 2 * * *', '/tick', 'paused', 'manual', %s)",
                (new_id("sch"), b.w.org, env, b.w.admin),
            )
        conn.execute("update ssc.environment set warm = true where name = 'prod'")
        conn.execute(
            "insert into ssc.connection (id, org_id, name, kind, classification, host, port, "
            "database_name, address) values (%s, %s, 'warehouse', 'postgres', 'internal', "
            "'db.corp.internal', 5432, 'warehouse', jsonb_build_object('host', "
            "'db.corp.internal', 'port', 5432, 'database', 'warehouse'))",
            (con, b.w.org),
        )
        for env in (b.w.prod, other_prod):
            conn.execute(
                "insert into ssc.connection_grant (id, org_id, connection_id, environment_id, "
                "created_by_user_id) values (%s, %s, %s, %s, %s)",
                (new_id("cgr"), b.w.org, con, env, b.w.admin),
            )
        conn.execute(
            "insert into ssc.github_installation (installation_id, org_id) values (7, %s)",
            (b.w.org,),
        )
        for app in (b.w.app, other):
            conn.execute(
                "insert into ssc.repo_link (org_id, app_id, installation_id, repository_id, "
                "repository, branch) values (%s, %s, 7, %s, 'acme/ledger', 'main')",
                (b.w.org, app, 11 if app == other else 12),
            )
        for approval, env in ((pending, b.w.prod), (theirs, other_prod)):
            conn.execute(
                "insert into ssc.approval_request (id, org_id, environment_id, kind, "
                "subject_key, requested_by_user_id) "
                "values (%s, %s, %s, 'connect_data_source', 'hr', %s)",
                (approval, b.w.org, env, b.w.builder),
            )

    run_id = deleting(b)
    assert (await drain(b, r.ports))[-1].outcome == "completed"
    removed = {s["name"]: s["removed"] for s in run_of(b, run_id)["steps"]}
    assert removed["remove_timers"] == 2
    assert removed["remove_records"] == 2 + 1 + 1 + 1  # grants, connection, repository, request

    states = rows_of(
        b.dsn,
        b.w.org,
        "select s.state, e.app_id from ssc.schedule s join ssc.environment e "
        "on e.id = s.environment_id order by e.app_id = %s",
        b.w.app,
    )
    assert [(s["state"], s["app_id"] == b.w.app) for s in states] == [
        ("paused", False),
        ("deleted", True),
        ("deleted", True),
    ]
    warm = rows_of(b.dsn, b.w.org, "select app_id from ssc.environment where warm")
    assert warm == [{"app_id": other}]
    assert count(b, "connection_grant", "environment_id = %s", other_prod) == 1
    assert count(b, "connection_grant") == 1 and count(b, "connection") == 1
    assert rows_of(b.dsn, b.w.org, "select app_id from ssc.repo_link") == [{"app_id": other}]
    requests = rows_of(
        b.dsn,
        b.w.org,
        "select id, state, decision_reason from ssc.approval_request order by id = %s",
        pending,
    )
    assert [(q["id"], q["state"]) for q in requests] == [
        (theirs, "pending"),
        (pending, "cancelled"),
    ]
    assert requests[1]["decision_reason"] == "The app was deleted."
    actions = rows_of(
        b.dsn,
        b.w.org,
        "select action, count(*) as n from ssc.audit_event where action in ('schedule.deleted', "
        "'connection.revoked', 'repo.disconnected', 'approval.cancelled') "
        "group by action order by action",
    )
    assert actions == [
        {"action": "approval.cancelled", "n": 1},
        {"action": "connection.revoked", "n": 1},
        {"action": "repo.disconnected", "n": 1},
        {"action": "schedule.deleted", "n": 2},
    ]


# ── the calls to a cell's agent ──────────────────────────────────────────────


async def tokens_for(url: str) -> str:
    return f"id-token-for-{url}"


def sent(**body: str) -> bytes:
    return httpx2.Request("POST", "https://agent.example", json=body).content


async def test_the_agent_clients_ask_the_cell_and_check_the_answer() -> None:
    answers: list[httpx2.Response] = []
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return answers.pop(0)

    def answer(status: int, **body: object) -> None:
        answers.append(httpx2.Response(status, json=body))

    org, service = "org_" + "a" * 20, service_name("env_" + "b" * 20)
    secret = secret_id(service, "STRIPE_KEY")
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    removal = AgentCellRemoval("https://agent.example/", tokens_for, org_id=org, client=client)
    databases = CellAppDatabases("https://agent.example/", tokens_for, org_id=org, client=client)

    answer(200, files=7)
    assert await removal.drop_files(service) == 7
    assert str(seen[-1].url) == "https://agent.example/v1/files/drop"
    assert seen[-1].headers["authorization"] == "Bearer id-token-for-https://agent.example"
    assert seen[-1].headers[ORG_HEADER] == org and seen[-1].content == sent(service=service)
    answer(200, removed=secret)
    assert await removal.remove_secret(secret) is True
    assert str(seen[-1].url) == "https://agent.example/v1/secrets/remove"
    assert seen[-1].content == sent(secret=secret)
    # A cell with no bucket, or that keeps no secrets, has nothing of that kind to delete.
    answer(503, code="FILES_NOT_CONFIGURED", message="no bucket")
    assert await removal.drop_files(service) == 0
    answer(503, code="SECRETS_NOT_CONFIGURED", message="no secrets")
    assert await removal.remove_secret(secret) is False
    # Anything else is a failure: a service still running, an answer that is not a count.
    answer(409, code="SERVICE_LIVE", message="still serving")
    with pytest.raises(CellRemovalError, match="409 SERVICE_LIVE"):
        await removal.drop_files(service)
    for wrong in ({}, {"files": -1}, {"files": True}, {"files": "3"}):
        answer(200, **wrong)
        with pytest.raises(CellRemovalError, match="not a count"):
            await removal.drop_files(service)
    answer(500, code="INTERNAL", message="boom")
    with pytest.raises(CellRemovalError):
        await removal.remove_secret(secret)

    answer(200, dropped=True)
    assert await databases.drop(service) is True
    assert str(seen[-1].url) == "https://agent.example/v1/databases/drop"
    assert seen[-1].content == sent(service=service)
    answer(503, code="DATABASES_NOT_CONFIGURED", message="no instance")
    assert await databases.drop(service) is False
    answer(409, code="SERVICE_LIVE", message="still serving")
    with pytest.raises(AppDatabaseError, match="409 SERVICE_LIVE") as refused:
        await databases.drop(service)
    assert refused.value.code == "DATABASE_UNAVAILABLE" and not refused.value.no_instance
    # No instance is only good news for a drop: making a database there is still a failure.
    answer(503, code="DATABASES_NOT_CONFIGURED", message="no instance")
    with pytest.raises(AppDatabaseError):
        await databases.ensure(service)
    await client.aclose()
