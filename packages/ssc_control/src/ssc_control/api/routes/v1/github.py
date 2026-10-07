"""A connected GitHub repository (SSC-047): ``/v1/apps/{app_id}/github``, the webhook GitHub
delivers to, and the required-checks gate promote passes.

Connecting names ``owner/name`` and optionally a branch (the repository's default branch when
omitted) and the checks promote requires. The repository must be reachable through a GitHub
App installation an SSC operator bound to the org (``REPOSITORY_NOT_INSTALLED`` otherwise).
Connecting, changing and disconnecting need a builder on prod, as promote does, because the
required checks gate prod; reading needs a builder on the app. Each change is audited as
``repo.connected`` or ``repo.disconnected`` with the repository's numeric id, never its name.

The webhook (``POST /v1/github/webhook``) is not in the OpenAPI document: GitHub is its only
caller, the ``X-Hub-Signature-256`` HMAC is the credential and a delivery without a valid one is
``UNAUTHENTICATED`` before anything else is read. A push to a connected branch defers one push
job per connected app (``github.push``); every other delivery, a pull request from a fork or
not among them, is answered ``ignored`` and builds nothing.
"""

import json
import logging
from datetime import datetime
from typing import Final

from fastapi import APIRouter, Request, Response
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_app_builder, require_builder
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.runtime import runtime_of
from ssc_control.api.uow import UnitOfWork, UserUoW
from ssc_control.db.bind import bound_org
from ssc_control.db.orgs import all_org_ids
from ssc_control.github import gate, links
from ssc_control.github.client import CHECK_NAME, GitHubApp, GitHubError, Repository
from ssc_control.github.links import BRANCH_PATTERN, REPOSITORY_PATTERN
from ssc_control.github.tasks import defer_push
from ssc_control.github.webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    MAX_BODY_BYTES,
    SIGNATURE_HEADER,
    Push,
    push_of,
    signature_ok,
)

log = logging.getLogger(__name__)
router = APIRouter()
webhook_router = APIRouter(include_in_schema=False)

WORKFLOW_PATTERN: Final = r"^\.github/workflows/[A-Za-z0-9._/-]+\.ya?ml$"
MAX_REQUIRED_CHECKS: Final = 10
NOT_FOUND_STATUS: Final = 404

_SELECT_PROD = text(
    "select e.id from ssc.app a join ssc.environment e on e.org_id = a.org_id "
    "and e.app_id = a.id and e.name = 'prod' where a.org_id = :org and a.id = :app"
)


class RequiredCheckIn(Strict):
    name: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[^\x00-\x1f\x7f]+$",
        description="The check run's name as GitHub shows it on the commit.",
    )
    workflow: str = Field(
        max_length=255,
        pattern=WORKFLOW_PATTERN,
        description="The workflow file the check run must come from, such as "
        "`.github/workflows/ci.yml`.",
    )


class RepoLinkIn(Strict):
    repository: str = Field(pattern=REPOSITORY_PATTERN, description="`owner/name`.")
    branch: str | None = Field(
        default=None,
        pattern=BRANCH_PATTERN,
        description="The branch whose pushes deploy preview; the repository's default branch "
        "when omitted.",
    )
    required_checks: list[RequiredCheckIn] = Field(
        default_factory=list[RequiredCheckIn],
        max_length=MAX_REQUIRED_CHECKS,
        description="Checks that must have passed on the commit, from a run of that workflow on "
        "the branch, before promote builds it for prod.",
    )


class RepoLinkOut(Strict):
    app_id: str
    repository: str
    repository_id: int
    branch: str
    required_checks: list[RequiredCheckIn]
    check_name: str = Field(description="The check run each push reports on its commit.")
    updated_at: datetime


class WebhookOut(Strict):
    status: str


async def _app_env(uow: UnitOfWork, app_id: str, *, change: bool) -> None:
    """``NOT_FOUND`` for an app the org does not have, then ``FORBIDDEN`` unless the caller is a
    builder on prod (``change``) or on the app. A change is a person's: the required checks are
    promote's gate, so an agent session is ``AGENT_SESSION_REFUSED``."""
    params = {"org": uow.org_id, "app": app_id}
    prod = (await uow.conn.execute(_SELECT_PROD, params)).scalar_one_or_none()
    if prod is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"app_id": app_id})
    if change:
        await require_builder(uow, str(prod))
        if uow.principal.is_agent:
            raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    else:
        await require_app_builder(uow, app_id)


def _out(link: links.Link) -> RepoLinkOut:
    return RepoLinkOut(
        app_id=link.app_id,
        repository=link.repository,
        repository_id=link.repository_id,
        branch=link.branch,
        required_checks=[
            RequiredCheckIn(name=c.name, workflow=c.workflow) for c in link.required_checks
        ],
        check_name=CHECK_NAME,
        updated_at=link.updated_at,
    )


def _github(request: Request) -> GitHubApp:
    github = runtime_of(request).github
    if github is None:
        raise Refusal(ErrorCode.GITHUB_UNAVAILABLE, evidence={"reason": "not_configured"})
    return github


async def _find(
    uow: UnitOfWork, github: GitHubApp, full_name: str
) -> tuple[int, Repository] | None:
    """The org's installation that reaches ``full_name``, and the repository."""
    for installation_id in await links.installations(uow.conn, uow.org_id):
        try:
            repo = await github.repository(installation_id, full_name)
        except GitHubError as e:
            if e.status == NOT_FOUND_STATUS:
                continue
            raise Refusal(
                ErrorCode.GITHUB_UNAVAILABLE, evidence={"status": e.status, "path": "repository"}
            ) from None
        if repo is not None:
            return installation_id, repo
    return None


@router.put(
    "/apps/{app_id}/github",
    response_model=RepoLinkOut,
    responses=problem_responses(
        *AUTHENTICATED,
        ErrorCode.FORBIDDEN,
        ErrorCode.AGENT_SESSION_REFUSED,
        ErrorCode.NOT_FOUND,
        ErrorCode.REPOSITORY_NOT_INSTALLED,
        ErrorCode.GITHUB_UNAVAILABLE,
    ),
)
async def connect_repository(
    app_id: Id, body: RepoLinkIn, request: Request, uow: UserUoW
) -> Response:
    """Connect the app to a repository, or change the branch or the required checks. Every
    push to the branch then builds that commit and deploys it to preview; prod still changes
    only through promote. Needs a builder on prod."""
    await _app_env(uow, app_id, change=True)
    found = await _find(uow, _github(request), body.repository)
    if found is None:
        raise Refusal(ErrorCode.REPOSITORY_NOT_INSTALLED, evidence={"app_id": app_id})
    installation_id, repo = found
    branch = body.branch or repo.default_branch
    if not links.valid_branch(branch) or not links.valid_repository(repo.full_name):
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"reason": "github_names"})
    before = await links.link_of(uow.conn, uow.org_id, app_id, lock=True)
    after = await links.upsert_link(
        uow.conn,
        uow.org_id,
        app_id,
        installation_id=installation_id,
        repository_id=repo.id,
        repository=repo.full_name,
        branch=branch,
        required_checks=[
            links.RequiredCheck(name=c.name, workflow=c.workflow) for c in body.required_checks
        ],
    )
    if before is None or before.view() != after.view():
        await uow.audit(
            AuditAction.REPO_CONNECTED,
            target_kind="repo_link",
            target_id=app_id,
            before=None if before is None else before.view(),
            after=after.view(),
        )
    return uow.reply(_out(after))


@router.get(
    "/apps/{app_id}/github",
    response_model=RepoLinkOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def get_repository(app_id: Id, uow: UserUoW) -> RepoLinkOut:
    """The connected repository; ``NOT_FOUND`` when none is connected."""
    await _app_env(uow, app_id, change=False)
    link = await links.link_of(uow.conn, uow.org_id, app_id)
    if link is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"app_id": app_id, "reason": "not_connected"})
    return _out(link)


@router.delete(
    "/apps/{app_id}/github",
    status_code=204,
    responses=problem_responses(
        *AUTHENTICATED,
        ErrorCode.FORBIDDEN,
        ErrorCode.AGENT_SESSION_REFUSED,
        ErrorCode.NOT_FOUND,
    ),
)
async def disconnect_repository(app_id: Id, uow: UserUoW) -> Response:
    """Disconnect the repository: pushes stop deploying preview and promote stops checking its
    required checks. What is deployed stays. Needs a builder on prod."""
    await _app_env(uow, app_id, change=True)
    link = await links.delete_link(uow.conn, uow.org_id, app_id)
    if link is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"app_id": app_id, "reason": "not_connected"})
    await uow.audit(
        AuditAction.REPO_DISCONNECTED, target_kind="repo_link", target_id=app_id, before=link.view()
    )
    return Response(status_code=204)


async def require_green_checks(
    uow: UnitOfWork,
    request: Request,
    app_id: str,
    source_commit: str | None,
    uploader: tuple[str, str] | None,
) -> None:
    """Promote's gate: every required check of the connected repository is green on the commit
    preview runs. ``REQUIRED_CHECKS_FAILING`` while one is not, when the release has no commit,
    or when its bundle (``uploader`` is that row's actor kind and id) was not made by the push
    job for the link's installation, since an uploaded bundle's commit is only what the client
    declared; ``GITHUB_UNAVAILABLE`` when GitHub cannot say. No connection or no required checks
    passes."""
    link = await links.link_of(uow.conn, uow.org_id, app_id)
    if link is None or not link.required_checks:
        return
    pusher = links.push_actor(link.installation_id)
    if uploader != (pusher.kind, pusher.id):
        raise Refusal(ErrorCode.REQUIRED_CHECKS_FAILING, evidence={"reason": "not_from_github"})
    if source_commit is None:
        raise Refusal(ErrorCode.REQUIRED_CHECKS_FAILING, evidence={"reason": "no_commit"})
    try:
        red = await gate.failing_checks(_github(request), link, source_commit)
    except GitHubError as e:
        raise Refusal(
            ErrorCode.GITHUB_UNAVAILABLE, evidence={"status": e.status, "path": "checks"}
        ) from None
    if red:
        raise Refusal(
            ErrorCode.REQUIRED_CHECKS_FAILING,
            evidence={"commit": source_commit, "failing": [c.label() for c in red]},
        )


async def _body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        raise Refusal(ErrorCode.UNAUTHENTICATED, evidence={"reason": "webhook_body_size"})
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise Refusal(ErrorCode.UNAUTHENTICATED, evidence={"reason": "webhook_body_size"})
        chunks.append(chunk)
    return b"".join(chunks)


async def _queue(request: Request, push: Push) -> int:
    """Defer a push job for each app connected to the pushed branch, in the org the
    installation is bound to; how many."""
    engine = runtime_of(request).engine
    for org_id in await all_org_ids(engine):
        async with bound_org(engine, org_id) as conn:
            if not await links.has_installation(conn, org_id, push.installation_id):
                continue
            pushed = await links.links_for_push(
                conn,
                org_id,
                installation_id=push.installation_id,
                repository_id=push.repository_id,
                branch=push.branch,
            )
            queued = 0
            for link in pushed:
                job = await defer_push(conn, org_id=org_id, app_id=link.app_id, sha=push.sha)
                queued += job is not None
            return queued
    return 0


@webhook_router.post("/github/webhook", response_model=WebhookOut)
async def github_webhook(request: Request) -> Response:
    """GitHub's deliveries. ``UNAUTHENTICATED`` unless the body carries the App's signature;
    ``202`` with ``queued`` when a push deferred a job, ``200`` with ``ignored`` otherwise."""
    body = await _body(request)
    secret = runtime_of(request).settings.github_webhook_secret
    if not signature_ok(secret, body, request.headers.get(SIGNATURE_HEADER)):
        raise Refusal(ErrorCode.UNAUTHENTICATED, evidence={"reason": "webhook_signature"})
    event = request.headers.get(EVENT_HEADER)
    try:
        payload: object = json.loads(body)
    except ValueError, RecursionError:
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"reason": "webhook_json"}) from None
    push = push_of(event, payload)
    queued = 0 if push is None else await _queue(request, push)
    log.info(
        "github delivery",
        extra={
            "delivery": request.headers.get(DELIVERY_HEADER),
            "event": event,
            "queued": queued,
        },
    )
    if queued:
        return _json({"status": "queued"}, 202)
    return _json({"status": "ignored"}, 200)


def _json(body: dict[str, str], status: int) -> Response:
    return Response(
        content=json.dumps(body, separators=(",", ":")),
        status_code=status,
        media_type="application/json",
    )
