"""The push job: one commit of a connected repository, stored, built and deployed to preview,
with the result and the preview address on the commit as the ``SSC / preview`` check run.

A step never sleeps: it does what it can and defers the next one :data:`POLL` later, carrying
the check run, build and deployment it made. The steps are

1. store and build: the tarball is packed like ``ssc deploy`` packs a folder
   (``github.source``) and stored, then every check ``complete`` runs runs on the stored object
   (``deploy.bundles.check_upload``: the tar, the manifest and the secret scan, unchanged). The
   bundle carries the commit as its source fingerprint (``source_commit``) and the build is
   queued for ``preview``. Nothing here ever builds or deploys ``prod``: that stays promote.
2. deploy: once the build succeeded and no deployment is in flight in preview, a forward deploy
   of its release, with preview's current config and sharing versions. A newer push's build
   wins: an older commit is never deployed over it.
3. report: the deployment's outcome. Healthy puts the preview address on the check run.

Every row it writes names the actor ``integration`` / ``github:<installation id>`` and is
audited like the API's own (``bundle.stored``, ``build.started`` with ``via: github``,
``deploy.started``). The job never imports API routes. A check run GitHub refuses is logged
with its status and path and the job goes on; a token never reaches a log.
"""

import asyncio
import json
import logging
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

from sqlalchemy import RowMapping, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_bundle.limits import DEFAULT_LIMITS, BundleError, BundleTooLargeError, Limits
from ssc_bundle.pack import PackedBundle
from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import CATALOGUE, ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.db.bind import bound_org
from ssc_control.deploy.bundle_gc import lock_bundle_key
from ssc_control.deploy.bundles import (
    DIGEST_PREFIX,
    BundleRejectedError,
    CheckedBundle,
    bundle_key,
    check_upload,
)
from ssc_control.deploy.tasks import defer_build, defer_deployment
from ssc_control.github.client import (
    CheckReport,
    GitHubApp,
    GitHubError,
    RepoRef,
    SourceTooLargeError,
)
from ssc_control.github.links import push_actor
from ssc_control.github.source import fetch_bundle
from ssc_control.github.tasks import defer_push
from ssc_control.storage import org_bundle_store
from ssc_control.worker_ports import Ports
from ssc_shared.blobstore import BlobError, BlobStore
from ssc_shared.hosts import app_origin, slug_problem

log = logging.getLogger(__name__)

POLL: Final = timedelta(seconds=10)
PREVIEW: Final = "preview"
NO_BLOB_STORE: Final = "BLOB_STORE_UNAVAILABLE"
SOURCE_UNAVAILABLE: Final = "SOURCE_UNAVAILABLE"
READ_CHUNK: Final = 1024 * 1024

_SELECT_CONTEXT = text(
    "select a.status, a.slug, o.cell_label, e.id as preview_id, l.installation_id, "
    "l.repository_id, l.repository from ssc.repo_link l "
    "join ssc.app a on a.org_id = l.org_id and a.id = l.app_id "
    "join ssc.org o on o.id = l.org_id "
    "join ssc.environment e on e.org_id = l.org_id and e.app_id = l.app_id and e.name = 'preview' "
    "where l.org_id = :org and l.app_id = :app"
)
_SELECT_BUILT = text(
    "select exists (select 1 from ssc.build b "
    "join ssc.bundle d on d.org_id = b.org_id and d.app_id = b.app_id and d.id = b.bundle_id "
    "where b.org_id = :org and b.environment_id = :env and b.actor_kind = 'integration' "
    "and d.source_commit = :sha)"
)
_INSERT_STORED = text(
    "insert into ssc.bundle (id, org_id, app_id, digest, size_bytes, source_commit, actor_kind, "
    "actor_id, state, manifest, manifest_digest, file_count, stored_at) values (:id, :org, :app, "
    ":digest, :size, :sha, 'integration', :actor, 'stored', cast(:manifest as jsonb), "
    ":manifest_digest, :file_count, now()) "
    "on conflict (org_id, app_id, digest) do nothing returning id"
)
_LOCK_BUNDLE = text(
    "select id, state, source_commit from ssc.bundle "
    "where org_id = :org and app_id = :app and digest = :digest for update"
)
_STORE_PENDING = text(
    "update ssc.bundle set state = 'stored', manifest = cast(:manifest as jsonb), "
    "manifest_digest = :manifest_digest, file_count = :file_count, stored_at = now() "
    "where org_id = :org and id = :id and state = 'pending'"
)
_INSERT_BUILD = text(
    "insert into ssc.build (id, org_id, app_id, environment_id, bundle_id, actor_kind, actor_id) "
    "values (:id, :org, :app, :env, :bundle, 'integration', :actor)"
)
_SELECT_BUILD = text(
    "select state, failure_code, release_id, environment_id from ssc.build "
    "where org_id = :org and id = :id"
)
_LOCK_ENV = text(
    "select config_version, grants_version from ssc.environment "
    "where org_id = :org and id = :env for update"
)
_SELECT_IN_FLIGHT = text(
    "select exists (select 1 from ssc.deployment where org_id = :org and environment_id = :env "
    "and state in ('pending', 'running'))"
)
_SELECT_NEWER = text(
    "select exists (select 1 from ssc.build n join ssc.build o "
    "on o.org_id = n.org_id and o.id = :id "
    "where n.org_id = :org and n.environment_id = o.environment_id and n.id <> o.id "
    "and n.actor_kind = 'integration' and n.created_at > o.created_at)"
)
_SELECT_APP_STATUS = text("select status from ssc.app where org_id = :org and id = :app")
_INSERT_DEPLOYMENT = text(
    "insert into ssc.deployment (id, org_id, app_id, environment_id, release_id, kind, state, "
    "config_version, grants_version, actor_kind, actor_id) values (:id, :org, :app, :env, :rel, "
    "'deploy', 'pending', :cv, :gv, 'integration', :actor)"
)
_SELECT_DEPLOYMENT = text(
    "select state, failure_code from ssc.deployment where org_id = :org and id = :id"
)


@dataclass(frozen=True, slots=True)
class _Push:
    """One step's inputs: the job's arguments and what the link says."""

    ports: Ports
    github: GitHubApp
    org_id: str
    app_id: str
    sha: str
    repo: RepoRef
    preview_id: str
    url: str | None
    app_status: str

    @property
    def actor(self) -> Actor:
        return push_actor(self.repo.installation_id)


def _failed(code: str) -> CheckReport:
    try:
        title = CATALOGUE[ErrorCode(code)].title
    except ValueError:
        title = "The preview was not deployed."
    return CheckReport(
        status="completed",
        conclusion="failure",
        title="Preview not deployed",
        summary=f"{title} Reason: `{code}`.",
    )


async def _report(p: _Push, check_run_id: int | None, report: CheckReport) -> int | None:
    """Create or update the check run; a GitHub refusal is logged and never stops the job."""
    try:
        if check_run_id is None:
            return await p.github.create_check_run(p.repo, p.sha, report)
        await p.github.update_check_run(p.repo, check_run_id, report)
    except GitHubError as e:
        log.warning(
            "check run not reported",
            extra={"org_id": p.org_id, "app_id": p.app_id, "status": e.status, "path": e.path},
        )
    return check_run_id


async def _again(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    p: _Push,
    *,
    check_run_id: int | None,
    build_id: str | None = None,
    deployment_id: str | None = None,
) -> None:
    await defer_push(
        conn,
        org_id=p.org_id,
        app_id=p.app_id,
        sha=p.sha,
        check_run_id=check_run_id,
        build_id=build_id,
        deployment_id=deployment_id,
        schedule_at=p.ports.clock() + POLL,
    )


async def _context(
    ports: Ports, github: GitHubApp, org_id: str, app_id: str, sha: str
) -> _Push | None:
    async with bound_org(ports.engine, org_id) as conn:
        row = (
            (await conn.execute(_SELECT_CONTEXT, {"org": org_id, "app": app_id})).mappings().first()
        )
    if row is None:
        return None
    slug = str(row["slug"])
    url = (
        None
        if slug_problem(slug) is not None
        else app_origin(slug, PREVIEW, str(row["cell_label"]), ports.apps_domain)
    )
    return _Push(
        ports=ports,
        github=github,
        org_id=org_id,
        app_id=app_id,
        sha=sha,
        repo=RepoRef(
            int(row["installation_id"]), int(row["repository_id"]), str(row["repository"])
        ),
        preview_id=str(row["preview_id"]),
        url=url,
        app_status=str(row["status"]),
    )


async def run_push(  # noqa: PLR0913  (keyword-only)
    ports: Ports,
    *,
    org_id: str,
    app_id: str,
    sha: str,
    check_run_id: int | None = None,
    build_id: str | None = None,
    deployment_id: str | None = None,
) -> str:
    """One step for ``sha`` of ``app_id``'s connected repository; returns where it got to."""
    if ports.github is None:
        log.warning("push not built: no GitHub App", extra={"org_id": org_id, "app_id": app_id})
        return "github_unavailable"
    p = await _context(ports, ports.github, org_id, app_id, sha)
    if p is None:
        return "disconnected"
    if deployment_id is not None:
        return await _after_deploy(p, check_run_id, deployment_id)
    if build_id is not None:
        return await _after_build(p, check_run_id, build_id)
    return await _start(p)


async def _start(p: _Push) -> str:
    if p.app_status != "active":
        await _report(p, None, _failed(ErrorCode.APP_NOT_ACTIVE.value))
        return "app_not_active"
    async with bound_org(p.ports.engine, p.org_id) as conn:
        params = {"org": p.org_id, "env": p.preview_id, "sha": p.sha}
        if (await conn.execute(_SELECT_BUILT, params)).scalar_one():
            return "duplicate"
    check_run_id = await _report(
        p,
        None,
        CheckReport(
            status="in_progress", title="Building", summary="Building this commit for preview."
        ),
    )
    store = await org_bundle_store(
        p.ports.engine, p.org_id, blob_store=p.ports.blob_store, cell_stores=p.ports.cell_stores
    )
    if store is None:
        await _report(p, check_run_id, _failed(NO_BLOB_STORE))
        return "failed"
    stored = await _store(p, store, DEFAULT_LIMITS)
    if isinstance(stored, str):
        await _report(p, check_run_id, _failed(stored))
        return "failed"
    digest, size, checked = stored
    async with bound_org(p.ports.engine, p.org_id) as conn:
        bundle_id = await _record_bundle(conn, p, digest, size, checked)
        build_id = await _queue_build(conn, p, bundle_id)
        if build_id is not None:
            await _again(conn, p, check_run_id=check_run_id, build_id=build_id)
    if build_id is None:
        await _report(p, check_run_id, _failed(ErrorCode.BUILD_IN_FLIGHT.value))
        return "failed"
    return "building"


async def _packed(p: _Push, workdir: Path, limits: Limits) -> PackedBundle | str:
    """The commit packed as a bundle, or the code that stopped it."""
    code = ErrorCode.BUNDLE_TOO_LARGE.value
    try:
        return await fetch_bundle(p.github, p.repo, p.sha, workdir, limits)
    except SourceTooLargeError, BundleTooLargeError:
        pass
    except GitHubError as e:
        log.warning(
            "source not downloaded",
            extra={"org_id": p.org_id, "app_id": p.app_id, "status": e.status},
        )
        code = SOURCE_UNAVAILABLE
    except BundleError as e:
        log.info("source refused", extra={"org_id": p.org_id, "reason": e.reason})
        code = ErrorCode.BUNDLE_MALFORMED.value
    return code


async def _store(
    p: _Push, store: BlobStore, limits: Limits
) -> tuple[str, int, CheckedBundle] | str:
    """The stored bundle's digest, size and checks, or the code that stopped it."""
    with tempfile.TemporaryDirectory(prefix="ssc-push-") as tmp:
        packed = await _packed(p, Path(tmp), limits)
        if isinstance(packed, str):
            return packed
        key = bundle_key(p.org_id, p.app_id, packed.digest)
        try:
            await store.put(
                key,
                _chunks(packed.path),
                size=packed.size,
                sha256=packed.digest.removeprefix(DIGEST_PREFIX),
            )
        except BlobError:
            log.warning("bundle not stored", extra={"org_id": p.org_id, "app_id": p.app_id})
            return NO_BLOB_STORE
    try:
        checked = await check_upload(
            store, key, size=packed.size, digest=packed.digest, limits=limits
        )
    except BundleRejectedError as e:
        log.info(
            "pushed bundle refused",
            extra={"org_id": p.org_id, "app_id": p.app_id, "code": e.code.value},
        )
        return e.code.value
    return packed.digest, packed.size, checked


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as f:
        while chunk := await asyncio.to_thread(f.read, READ_CHUNK):
            yield chunk


async def _record_bundle(
    conn: AsyncConnection, p: _Push, digest: str, size: int, checked: CheckedBundle
) -> str:
    """The stored bundle's id: a new row, a pending row now stored, or the stored row there
    already. Audited as ``bundle.stored`` when it becomes stored here."""
    await lock_bundle_key(conn, bundle_key(p.org_id, p.app_id, digest))
    manifest = json.dumps(checked.manifest.model_dump(mode="json", by_alias=True))
    params = {
        "org": p.org_id,
        "app": p.app_id,
        "digest": digest,
        "manifest": manifest,
        "manifest_digest": checked.manifest_digest,
        "file_count": checked.file_count,
    }
    inserted = (
        await conn.execute(
            _INSERT_STORED,
            {**params, "id": new_id("bdl"), "size": size, "sha": p.sha, "actor": p.actor.id},
        )
    ).scalar_one_or_none()
    commit: str | None = p.sha
    if inserted is not None:
        bundle_id = str(inserted)
    else:
        row = (await conn.execute(_LOCK_BUNDLE, params)).mappings().one()
        bundle_id, commit = str(row["id"]), row["source_commit"]
        if row["state"] == "stored":
            return bundle_id
        await conn.execute(_STORE_PENDING, {**params, "id": bundle_id})
    await _audit(
        conn,
        p,
        action=AuditAction.BUNDLE_STORED,
        target_kind="bundle",
        target_id=bundle_id,
        after={
            "app_id": p.app_id,
            "digest": digest,
            "size_bytes": size,
            "file_count": checked.file_count,
            "manifest_digest": checked.manifest_digest,
            "source_commit": commit,
        },
    )
    return bundle_id


async def _queue_build(conn: AsyncConnection, p: _Push, bundle_id: str) -> str | None:
    """Queue the preview build and its job; None while the same bundle builds for preview."""
    build_id = new_id("bld")
    try:
        async with conn.begin_nested():
            await conn.execute(
                _INSERT_BUILD,
                {
                    "id": build_id,
                    "org": p.org_id,
                    "app": p.app_id,
                    "env": p.preview_id,
                    "bundle": bundle_id,
                    "actor": p.actor.id,
                },
            )
    except IntegrityError:
        return None
    await _audit(
        conn,
        p,
        action=AuditAction.BUILD_STARTED,
        target_kind="build",
        target_id=build_id,
        after={
            "environment_id": p.preview_id,
            "bundle_id": bundle_id,
            "state": "queued",
            "via": "github",
        },
    )
    await defer_build(conn, org_id=p.org_id, build_id=build_id)
    return build_id


async def _audit(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    p: _Push,
    *,
    action: AuditAction,
    target_kind: str,
    target_id: str,
    after: dict[str, object],
) -> None:
    await append_event(
        conn,
        NewEvent(
            org_id=p.org_id,
            action=action,
            actor=p.actor,
            target_kind=target_kind,
            target_id=target_id,
            after=after,
        ),
    )


async def _after_build(p: _Push, check_run_id: int | None, build_id: str) -> str:
    async with bound_org(p.ports.engine, p.org_id) as conn:
        build = (
            (await conn.execute(_SELECT_BUILD, {"org": p.org_id, "id": build_id}))
            .mappings()
            .first()
        )
        if build is None:
            return "missing"
        if build["state"] in {"queued", "running"}:
            await _again(conn, p, check_run_id=check_run_id, build_id=build_id)
            return "building"
        if build["state"] == "succeeded":
            outcome = await _deploy(conn, p, check_run_id, build_id, build)
        else:
            outcome = str(build["failure_code"])
    if outcome == "superseded":
        await _report(
            p,
            check_run_id,
            CheckReport(
                status="completed",
                conclusion="neutral",
                title="Superseded",
                summary="A newer push to this branch deploys preview instead.",
            ),
        )
        return outcome
    if outcome in {"deploying", "waiting"}:
        if outcome == "deploying":
            await _report(
                p,
                check_run_id,
                CheckReport(status="in_progress", title="Deploying", summary="Deploying preview."),
            )
        return outcome
    await _report(p, check_run_id, _failed(outcome))
    return "failed"


async def _deploy(
    conn: AsyncConnection, p: _Push, check_run_id: int | None, build_id: str, build: RowMapping
) -> str:
    """Start the preview deployment of the build's release: ``deploying``, ``waiting`` while
    another deployment is in flight, ``superseded`` or the code that stops it."""
    params = {"org": p.org_id, "app": p.app_id, "env": p.preview_id, "id": build_id}
    env = (await conn.execute(_LOCK_ENV, params)).mappings().one()
    if (await conn.execute(_SELECT_APP_STATUS, params)).scalar_one() != "active":
        return ErrorCode.APP_NOT_ACTIVE.value
    if (await conn.execute(_SELECT_NEWER, params)).scalar_one():
        return "superseded"
    if (await conn.execute(_SELECT_IN_FLIGHT, params)).scalar_one():
        await _again(conn, p, check_run_id=check_run_id, build_id=build_id)
        return "waiting"
    deployment_id = new_id("dep")
    release_id = str(build["release_id"])
    await conn.execute(
        _INSERT_DEPLOYMENT,
        {
            **params,
            "id": deployment_id,
            "rel": release_id,
            "cv": env["config_version"],
            "gv": env["grants_version"],
            "actor": p.actor.id,
        },
    )
    await _audit(
        conn,
        p,
        action=AuditAction.DEPLOY_STARTED,
        target_kind="deployment",
        target_id=deployment_id,
        after={"environment_id": p.preview_id, "release_id": release_id},
    )
    await defer_deployment(
        conn, org_id=p.org_id, environment_id=p.preview_id, deployment_id=deployment_id
    )
    await _again(conn, p, check_run_id=check_run_id, deployment_id=deployment_id)
    return "deploying"


async def _after_deploy(p: _Push, check_run_id: int | None, deployment_id: str) -> str:
    async with bound_org(p.ports.engine, p.org_id) as conn:
        dep = (
            (await conn.execute(_SELECT_DEPLOYMENT, {"org": p.org_id, "id": deployment_id}))
            .mappings()
            .first()
        )
        if dep is None:
            return "missing"
        if dep["state"] in {"pending", "running"}:
            await _again(conn, p, check_run_id=check_run_id, deployment_id=deployment_id)
            return "deploying"
    if dep["state"] == "healthy":
        summary = f"Preview: {p.url}" if p.url else "Deployed to preview."
        await _report(
            p,
            check_run_id,
            CheckReport(
                status="completed",
                conclusion="success",
                title="Preview deployed",
                summary=summary,
                details_url=p.url,
            ),
        )
        return "healthy"
    code = dep["failure_code"] or ("SUPERSEDED" if dep["state"] == "superseded" else "FAILED")
    await _report(p, check_run_id, _failed(str(code)))
    return str(dep["state"])
