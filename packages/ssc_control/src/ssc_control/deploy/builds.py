"""One build, from ``queued`` to a release or a reason code (SSC-016).

``run_build`` is the whole job and is safe to run twice: each state change is a compare-and-set
on the build row, and ``BuildDriver.start`` is keyed by the build id. While the builder works the
job defers its own next poll (``schedule_at``) instead of sleeping, so a build never holds a
worker, and no transaction stays open across a builder call. A builder error is retried until
the build's deadline; after it, the build fails with ``BUILD_TIMED_OUT`` or
``BUILD_DRIVER_ERROR``, so no build stays ``running``. Every step starts by claiming the build
and re-reading its app: a disabled or quarantined app fails the build with ``APP_NOT_ACTIVE``
before any builder call.

Before the builder is first called the job reads the stored bundle (``ssc_bundle.analyze``,
SSC-015): a refusal fails the build with its code (``STATE_SQLITE_EPHEMERAL``,
``BUILD_PRIVATE_REGISTRY`` and the rest of ``ssc_contracts.build``), notices go to the log with
the build id, and the session framework found is kept on the build and then on its release, where
``desired_for`` reads it. The analysis needs the blob store; only a development or test
composition with the fake builder runs without one, and then skips it.

On success one transaction numbers and writes the release (``source_digest`` is the bundle's
digest, ``manifest_digest`` the stored bundle's), marks the build ``succeeded`` and audits
``release.created`` as the build's actor.
"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.manifest import Manifest
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.db.bind import bound_org
from ssc_control.deploy.build_driver import (
    BUILD_DRIVER_ERROR,
    BUILD_TIMED_OUT,
    BuildDriver,
    BuildNotFoundError,
    BuildRequest,
    BuildStatus,
    Failed,
    Running,
    Succeeded,
)
from ssc_control.deploy.bundles import analyze_stored, bundle_key
from ssc_control.deploy.releases import NewRelease, allocate_and_insert
from ssc_control.deploy.tasks import defer_build
from ssc_control.worker_ports import Ports
from ssc_shared.canonical import manifest_digest

log = logging.getLogger(__name__)

BUILD_TIMEOUT: Final = timedelta(minutes=20)
POLL_INTERVAL: Final = timedelta(seconds=10)
BUILD_DRIVER_UNAVAILABLE: Final = "BUILD_DRIVER_UNAVAILABLE"
NOT_RUNNING: Final = "not_running"
"""What a step returns when another run of the job already finished the build."""

_LOAD = text(
    "select b.state, b.app_id, b.environment_id, b.bundle_id, b.driver_ref, b.started_at, "
    "b.framework, b.actor_kind, b.actor_id, b.actor_via_agent, b.actor_client_id, "
    "e.name as env_name, "
    "d.digest, d.manifest, d.manifest_digest, d.source_commit, a.status as app_status "
    "from ssc.build b "
    "join ssc.environment e on e.org_id = b.org_id and e.id = b.environment_id "
    "join ssc.app a on a.org_id = b.org_id and a.id = b.app_id "
    "join ssc.bundle d on d.org_id = b.org_id and d.app_id = b.app_id and d.id = b.bundle_id "
    "where b.org_id = :org and b.id = :id for update of b"
)
_CLAIM = text(
    "update ssc.build set state = 'running', started_at = now() "
    "where org_id = :org and id = :id and state = 'queued' returning started_at"
)
_SET_REF = text(
    "update ssc.build set driver_ref = :ref "
    "where org_id = :org and id = :id and state = 'running' and driver_ref is null"
)
_SET_FRAMEWORK = text(
    "update ssc.build set framework = :framework "
    "where org_id = :org and id = :id and state = 'running' and driver_ref is null"
)
_LOCK_RUNNING = text(
    "select 1 from ssc.build where org_id = :org and id = :id and state = 'running' for update"
)
_SUCCEED = text(
    "update ssc.build set state = 'succeeded', release_id = :rel, finished_at = now() "
    "where org_id = :org and id = :id and state = 'running'"
)
_FAIL = text(
    "update ssc.build set state = 'failed', failure_code = :code, finished_at = now() "
    "where org_id = :org and id = :id and state = 'running'"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Build:
    id: str
    state: str
    app_id: str
    environment_id: str
    bundle_id: str
    driver_ref: str | None
    started_at: datetime | None
    framework: str | None
    actor: Actor
    env_name: str
    digest: str
    manifest: Any
    manifest_digest: str | None
    source_commit: str | None
    app_status: str


def _actor(row: Any) -> Actor:
    return Actor(
        ActorKind(str(row.actor_kind)),
        str(row.actor_id),
        via_agent=bool(row.actor_via_agent),
        client_id=None if row.actor_client_id is None else str(row.actor_client_id),
    )


async def _load(conn: AsyncConnection, org_id: str, build_id: str) -> _Build | None:
    row = (await conn.execute(_LOAD, {"org": org_id, "id": build_id})).first()
    if row is None:
        return None
    return _Build(
        id=build_id,
        state=str(row.state),
        app_id=str(row.app_id),
        environment_id=str(row.environment_id),
        bundle_id=str(row.bundle_id),
        driver_ref=row.driver_ref,
        started_at=row.started_at,
        framework=row.framework,
        actor=_actor(row),
        env_name=str(row.env_name),
        digest=str(row.digest),
        manifest=row.manifest,
        manifest_digest=row.manifest_digest,
        source_commit=row.source_commit,
        app_status=str(row.app_status),
    )


def _request(org_id: str, build: _Build) -> BuildRequest | None:
    """What the builder gets, or None when the stored manifest does not check out."""
    if build.manifest is None or build.manifest_digest is None:
        return None
    try:
        manifest = Manifest.model_validate(build.manifest)
    except ValidationError:
        return None
    if manifest_digest(manifest) != build.manifest_digest:
        return None
    return BuildRequest(
        build_id=build.id,
        org_id=org_id,
        app_id=build.app_id,
        env_name=build.env_name,
        bundle_key=bundle_key(org_id, build.app_id, build.digest),
        source_digest=build.digest,
        manifest=manifest,
        public_env=next(
            (v for k, v in manifest.build.public_env.items() if k == build.env_name),
            dict[str, str](),
        ),
    )


async def run_build(
    ports: Ports,
    *,
    org_id: str,
    build_id: str,
    build_timeout: timedelta = BUILD_TIMEOUT,
    poll_interval: timedelta = POLL_INTERVAL,
) -> str:
    """One step of the build job; the build's state after it, ``missing`` or ``not_running``."""
    build = await _claim(ports, org_id, build_id)
    if isinstance(build, str):
        return build
    driver = ports.build_driver
    if driver is None:
        return await _fail(ports, org_id, build, BUILD_DRIVER_UNAVAILABLE)
    request = _request(org_id, build)
    if request is None:
        return await _fail(ports, org_id, build, ErrorCode.MANIFEST_INVALID.value)
    checked = await _check_source(ports, org_id, build, request)
    if isinstance(checked, _Build):
        build, status = checked, await _step(ports, driver, org_id, checked, request)
    else:
        status = checked
    return await _settle(
        ports, org_id, build, status, build_timeout=build_timeout, poll_interval=poll_interval
    )


async def _settle(  # noqa: PLR0913  (keyword-only)
    ports: Ports,
    org_id: str,
    build: _Build,
    status: BuildStatus | None,
    *,
    build_timeout: timedelta,
    poll_interval: timedelta,
) -> str:
    """Act on what the builder said: a release, a failure, or the next poll."""
    match status:
        case Succeeded():
            return await _succeed(ports, org_id, build, status)
        case Failed(code=code, message=message):
            log.info("build failed", extra={"build_id": build.id, "code": code, "why": message})
            return await _fail(ports, org_id, build, code)
        case Running() | None:
            if ports.clock() >= (build.started_at or ports.clock()) + build_timeout:
                code = BUILD_DRIVER_ERROR if status is None else BUILD_TIMED_OUT
                return await _fail(ports, org_id, build, code)
            return await _poll_later(ports, org_id, build.id, ports.clock() + poll_interval)


async def _claim(ports: Ports, org_id: str, build_id: str) -> _Build | str:
    """The running build (claimed now, or by an earlier run), or the state it ended in: already,
    or now with ``APP_NOT_ACTIVE`` when its app is stopped."""
    async with bound_org(ports.engine, org_id) as conn:
        build = await _load(conn, org_id, build_id)
        if build is None:
            log.warning("build not found", extra={"org_id": org_id, "build_id": build_id})
            return "missing"
        if build.state in ("succeeded", "failed"):
            return build.state
        if build.state == "queued":
            started = (await conn.execute(_CLAIM, {"org": org_id, "id": build_id})).scalar_one()
            build = replace(build, state="running", started_at=started)
        if build.app_status != "active":
            return await _fail_in(conn, org_id, build, ErrorCode.APP_NOT_ACTIVE.value)
    return build


async def _check_source(
    ports: Ports, org_id: str, build: _Build, request: BuildRequest
) -> _Build | Failed | None:
    """Analyse the bundle before the builder is first called: the build with its framework, a
    refusal, or None when the bundle could not be read (try again later)."""
    store = ports.blob_store
    if store is None or build.driver_ref is not None:
        return build
    try:
        found = await analyze_stored(store, request.bundle_key, request.manifest)
    except Exception:
        log.exception("bundle analysis failed", extra={"build_id": build.id})
        return None
    if found.notices:
        log.info("build notices", extra={"build_id": build.id, "notices": list(found.notices)})
    if found.refusal is not None:
        why = f"{found.refusal.detail} ({found.refusal.path})"
        return Failed(code=found.refusal.code, message=why)
    if found.framework is not None:
        async with bound_org(ports.engine, org_id) as conn:
            params = {"org": org_id, "id": build.id, "framework": found.framework}
            await conn.execute(_SET_FRAMEWORK, params)
    return replace(build, framework=found.framework)


async def _step(
    ports: Ports, driver: BuildDriver, org_id: str, build: _Build, request: BuildRequest
) -> BuildStatus | None:
    """Start the build if it has no reference yet, then poll it. None: the builder errored, so
    try again later. An unknown reference is a failure: the builder lost the build."""
    ref = build.driver_ref
    try:
        if ref is None:
            ref = await driver.start(request)
            async with bound_org(ports.engine, org_id) as conn:
                await conn.execute(_SET_REF, {"org": org_id, "id": build.id, "ref": ref})
        return await driver.poll(ref)
    except BuildNotFoundError:
        log.warning("builder lost the build", extra={"build_id": build.id, "ref": ref})
        return Failed(code=BUILD_DRIVER_ERROR, message="the builder has no such build")
    except Exception:
        log.exception("build driver call failed", extra={"build_id": build.id})
        return None


async def _poll_later(ports: Ports, org_id: str, build_id: str, at: datetime) -> str:
    async with bound_org(ports.engine, org_id) as conn:
        if (await conn.execute(_LOCK_RUNNING, {"org": org_id, "id": build_id})).first() is None:
            return NOT_RUNNING
        await defer_build(conn, org_id=org_id, build_id=build_id, schedule_at=at)
    return "running"


async def _succeed(ports: Ports, org_id: str, build: _Build, result: Succeeded) -> str:
    async with bound_org(ports.engine, org_id) as conn:
        params = {"org": org_id, "id": build.id}
        if (await conn.execute(_LOCK_RUNNING, params)).first() is None:
            return NOT_RUNNING
        if build.manifest_digest is None:
            raise AssertionError("a running build's bundle is stored")
        release = NewRelease(
            app_id=build.app_id,
            image_digest=result.image_digest,
            manifest_digest=build.manifest_digest,
            source_digest=build.digest,
            source_commit=build.source_commit,
            scan_refs=result.scan_refs,
            framework=build.framework,
            actor=build.actor,
        )
        allocated = await allocate_and_insert(conn, org_id=org_id, release=release)
        await conn.execute(_SUCCEED, {**params, "rel": allocated.id})
        await append_event(
            conn,
            NewEvent(
                org_id=org_id,
                action=AuditAction.RELEASE_CREATED,
                actor=build.actor,
                target_kind="release",
                target_id=allocated.id,
                after={
                    "number": allocated.number,
                    "image_digest": release.image_digest,
                    "manifest_digest": release.manifest_digest,
                    "source_digest": release.source_digest,
                    "source_commit": release.source_commit,
                },
            ),
        )
    log.info("build succeeded", extra={"build_id": build.id, "release_id": allocated.id})
    return "succeeded"


async def _fail(ports: Ports, org_id: str, build: _Build, code: str) -> str:
    async with bound_org(ports.engine, org_id) as conn:
        return await _fail_in(conn, org_id, build, code)


async def _fail_in(conn: AsyncConnection, org_id: str, build: _Build, code: str) -> str:
    done = await conn.execute(_FAIL, {"org": org_id, "id": build.id, "code": code})
    if done.rowcount == 0:
        return NOT_RUNNING
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.BUILD_FAILED,
            actor=build.actor,
            target_kind="build",
            target_id=build.id,
            before={"state": "running"},
            after={"state": "failed", "failure_code": code},
        ),
    )
    return "failed"
