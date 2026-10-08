"""HTTP client for ``/v1`` over httpx2.

* GET and POST are retried up to three times with backoff on connection errors and 5xx. Every
  POST carries one ``Idempotency-Key`` per logical operation, reused on each retry, so a retry
  can never do the work twice. A keyed POST also waits out ``IDEMPOTENCY_IN_FLIGHT``.
* PUT is conditional (``If-Match``) and is not retried here; callers handle ``412`` themselves.
  ``PUT .../grants`` answers ``202`` with the pending approval ids when the change needs approval.
* ``429`` is retried after each ``Retry-After``: the API refuses before doing any work, so any
  method may be sent again. Once one request has waited ``RATE_WAIT_SECONDS`` in all, the next
  ``429`` raises, carrying its wait (at most ``MAX_RETRY_AFTER``) as ``CliError.retry_after``.
  Ten deploys from one login (cell 1, 2026-10-07) each got through this way.
* A refusal becomes a :class:`~ssc_cli.errors.CliError` carrying the API's problem members.
* Every request names the tool in ``X-SSC-Source-Tool`` for the API's source tool mix.
* A bundle goes to the signed upload URL the API hands out, from a separate client that sends
  no token. The URL is a credential and never appears in an error.
* A secret's value goes the same way, to the cell's secret intake with the grant the API hands
  out, and never to the API. Neither the value nor the grant appears in an error.
"""

import json
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self, cast
from urllib.parse import quote, urlencode

import httpx2
from pydantic import BaseModel, ValidationError

from ssc_cli import __version__
from ssc_cli.errors import (
    BAD_RESPONSE,
    NETWORK_ERROR,
    UPLOAD_FAILED,
    CliError,
    ErrorBody,
    ExitCode,
    api_error,
    local_error,
)
from ssc_cli.models import (
    AccessExplained,
    AppCreate,
    AppList,
    AppOut,
    ApprovalDecided,
    ApprovalDetail,
    ApprovalPage,
    BuildAccepted,
    BuildCreate,
    BuildOut,
    BundleCreate,
    BundleOut,
    ConnectionsOut,
    DatabaseOut,
    DatabaseRotateOut,
    DeploymentCreate,
    DeploymentPolicy,
    EnvironmentConnectionsOut,
    GrantIn,
    GrantsIn,
    GrantsOut,
    GrantsPending,
    GroupMatches,
    HealthOut,
    KillSwitchAccepted,
    KillSwitchCreate,
    KillSwitchRun,
    Linked,
    LinkIn,
    LogPageOut,
    MigrationsAhead,
    OperationAccepted,
    OperationOut,
    PersonDecisionIn,
    PromoteIn,
    ReleaseList,
    ReleaseOut,
    SecretGrantOut,
    SecretList,
    SecretSet,
    SecretSetOut,
    UnlinkedLogins,
    UploadTarget,
    UsageOut,
    UserMatches,
    Whoami,
)
from ssc_contracts.errors import ErrorCode

USER_AGENT: Final = f"ssc-cli/{__version__}"
SOURCE_TOOL_HEADER: Final = "X-SSC-Source-Tool"
SOURCE_TOOL: Final = "ssc-cli"
IDEMPOTENCY_HEADER: Final = "Idempotency-Key"
IF_MATCH: Final = "If-Match"
ETAG: Final = "ETag"
REQUEST_ID_HEADER: Final = "X-Request-Id"
BACKOFF: Final = (0.5, 1.0, 2.0)
MAX_RETRY_AFTER: Final = 60.0
RATE_WAIT_SECONDS: Final = 120.0
DEFAULT_TIMEOUT: Final = 30.0
UPLOAD_TIMEOUT: Final = 300.0
UPLOAD_CHUNK: Final = 1024 * 1024
ALREADY_STORED: Final = 412

Sleep = Callable[[float], None]


def _seg(value: str) -> str:
    return quote(value, safe="")


class ApiClient:
    """One API address and one token, or a callable that gives the current one (a login, whose
    access token is replaced every few minutes). Use as a context manager."""

    def __init__(
        self,
        base_url: str,
        token: str | Callable[[], str],
        *,
        transport: httpx2.BaseTransport | None = None,
        sleep: Sleep = time.sleep,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_url = base_url
        self._token = token
        self._sleep = sleep
        self._transport = transport
        self._upload_http: httpx2.Client | None = None
        self._http = httpx2.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={
                "User-Agent": USER_AGENT,
                SOURCE_TOOL_HEADER: SOURCE_TOOL,
                "Accept": "application/json",
            },
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._upload_http is not None:
            self._upload_http.close()
        self._http.close()

    # ── endpoints ────────────────────────────────────────────────────────────

    def whoami(self) -> Whoami:
        return _parse(self._send("GET", "/v1/whoami"), Whoami)

    def deployment_policy(self) -> DeploymentPolicy:
        return _parse(self._send("GET", "/v1/org/deployment-policy"), DeploymentPolicy)

    def list_connections(self) -> ConnectionsOut:
        """The data connections the caller may see."""
        return _parse(self._send("GET", "/v1/connections"), ConnectionsOut)

    def environment_connections(
        self, app_id: str, environment_id: str
    ) -> EnvironmentConnectionsOut:
        """The connections one environment may reach, of those the caller may see."""
        path = f"/v1/apps/{_seg(app_id)}/environments/{_seg(environment_id)}/connections"
        return _parse(self._send("GET", path), EnvironmentConnectionsOut)

    def list_approvals(self, *, inbox: bool, limit: int = 50) -> ApprovalPage:
        """The requests the caller may decide (``inbox``), or every one they may see."""
        query = f"?limit={limit}" + ("&inbox=true" if inbox else "")
        return _parse(self._send("GET", f"/v1/approvals{query}"), ApprovalPage)

    def get_approval(self, approval_id: str) -> ApprovalDetail:
        return _parse(self._send("GET", f"/v1/approvals/{_seg(approval_id)}"), ApprovalDetail)

    def decide_approval(self, approval_id: str, outcome: str, reason: str) -> ApprovalDecided:
        """Approve (``approved``) or reject (``denied``) a request, as the caller."""
        body = PersonDecisionIn(outcome=outcome, reason=reason)
        path = f"/v1/approvals/{_seg(approval_id)}/decide"
        return _parse(self._send("POST", path, body=body), ApprovalDecided)

    def list_apps(self, *, mine: bool = False) -> AppList:
        """Every app of the org, or with ``mine`` only those the caller may deploy to."""
        return _parse(self._send("GET", "/v1/apps?builder=me" if mine else "/v1/apps"), AppList)

    def get_app(self, app_id: str) -> AppOut:
        return _parse(self._send("GET", f"/v1/apps/{_seg(app_id)}"), AppOut)

    def create_app(self, slug: str) -> AppOut:
        return _parse(self._send("POST", "/v1/apps", body=AppCreate(slug=slug)), AppOut)

    def get_operation(self, operation_id: str) -> OperationOut:
        return _parse(self._send("GET", f"/v1/operations/{_seg(operation_id)}"), OperationOut)

    def get_grants(self, app_id: str, environment_id: str) -> tuple[GrantsOut, str]:
        """The sharing rules and the ETag to send back in ``If-Match``."""
        r = self._send("GET", _grants_path(app_id, environment_id))
        out = _parse(r, GrantsOut)
        return out, r.headers.get(ETAG) or f'"{out.grants_version}"'

    def put_grants(
        self, app_id: str, environment_id: str, grants: list[GrantIn], if_match: str
    ) -> GrantsOut | GrantsPending:
        """The new sharing rules, or, on ``202``, the approvals the change waits for."""
        r = self._send(
            "PUT",
            _grants_path(app_id, environment_id),
            body=GrantsIn(grants=grants),
            headers={IF_MATCH: if_match},
        )
        if r.status_code == 202:
            return _parse(r, GrantsPending)
        return _parse(r, GrantsOut)

    def create_bundle(self, app_id: str, body: BundleCreate) -> BundleOut:
        """The bundle recorded by digest: ``upload`` is set while its bytes are still needed."""
        return _parse(self._send("POST", f"/v1/apps/{_seg(app_id)}/bundles", body=body), BundleOut)

    def complete_bundle(self, app_id: str, bundle_id: str) -> BundleOut:
        path = f"/v1/apps/{_seg(app_id)}/bundles/{_seg(bundle_id)}/complete"
        return _parse(self._send("POST", path), BundleOut)

    def upload(self, target: UploadTarget, path: Path) -> None:
        """PUT the file to the signed URL, streaming it with its exact length, the headers the
        API asked for and without the API token. Transport errors and 5xx are retried like a GET.
        A 412 means a bucket already holds an object there (its URLs only create), so ``complete``
        decides whether it is these bytes."""
        upload = self._upload_client()
        asked = {k: v for k, v in target.headers.items() if k.lower() != "content-length"}
        headers = {**asked, "Content-Length": str(path.stat().st_size), "User-Agent": USER_AGENT}
        retries = 0
        while True:
            try:
                r = upload.request(
                    target.method,
                    target.url,
                    content=_chunks(path),
                    headers=headers,
                )
            except httpx2.TransportError as e:
                if retries < len(BACKOFF):
                    self._sleep(BACKOFF[retries])
                    retries += 1
                    continue
                raise local_error(
                    NETWORK_ERROR,
                    "The bundle could not be uploaded.",
                    f"{type(e).__name__} while uploading. Check your connection, then retry.",
                    ExitCode.NETWORK,
                ) from None
            if r.status_code >= 500 and retries < len(BACKOFF):
                self._sleep(BACKOFF[retries])
                retries += 1
                continue
            if r.status_code == ALREADY_STORED:
                return
            if r.status_code >= 300:
                raise local_error(
                    UPLOAD_FAILED,
                    "The bundle upload was refused.",
                    f"The upload address answered HTTP {r.status_code}. Run the command again; "
                    "it asks for a fresh address.",
                )
            return

    def _upload_client(self) -> httpx2.Client:
        """A client with no API token, for addresses the API hands out."""
        if self._upload_http is None:
            self._upload_http = httpx2.Client(
                transport=self._transport, timeout=UPLOAD_TIMEOUT, follow_redirects=False
            )
        return self._upload_http

    def create_build(self, app_id: str, environment_id: str, bundle_id: str) -> BuildAccepted:
        path = f"{_environment_path(app_id, environment_id)}/builds"
        body = BuildCreate(bundle_id=bundle_id)
        return _parse(self._send("POST", path, body=body), BuildAccepted)

    def get_build(self, build_id: str) -> BuildOut:
        return _parse(self._send("GET", f"/v1/builds/{_seg(build_id)}"), BuildOut)

    def create_deployment(
        self, app_id: str, environment_id: str, release_id: str, kind: str, *, confirm: bool = False
    ) -> OperationAccepted:
        path = f"{_environment_path(app_id, environment_id)}/deployments"
        body = DeploymentCreate(release_id=release_id, kind=kind, confirm=confirm)
        return _parse(self._send("POST", path, body=body), OperationAccepted)

    def migrations_ahead(
        self, app_id: str, environment_id: str, release_id: str
    ) -> MigrationsAhead:
        """The migrations the environment's database may have run that the release lacks."""
        query = urlencode({"release_id": release_id})
        path = f"{_environment_path(app_id, environment_id)}/migrations-ahead?{query}"
        return _parse(self._send("GET", path), MigrationsAhead)

    def promote(self, app_id: str, preview_release_id: str | None) -> BuildAccepted:
        body = PromoteIn(preview_release_id=preview_release_id)
        path = f"/v1/apps/{_seg(app_id)}/promote"
        return _parse(self._send("POST", path, body=body), BuildAccepted)

    def list_releases(self, app_id: str, *, limit: int, before: int | None = None) -> ReleaseList:
        query = f"?limit={limit}" + ("" if before is None else f"&before={before}")
        return _parse(self._send("GET", f"/v1/apps/{_seg(app_id)}/releases{query}"), ReleaseList)

    def get_release(self, app_id: str, release_id: str) -> ReleaseOut:
        path = f"/v1/apps/{_seg(app_id)}/releases/{_seg(release_id)}"
        return _parse(self._send("GET", path), ReleaseOut)

    def find_users(self, email: str) -> UserMatches:
        """The org's people with this address, deactivated ones included. Org admins only."""
        return _parse(self._send("GET", f"/v1/users?{urlencode({'email': email})}"), UserMatches)

    def list_unlinked_logins(self) -> UnlinkedLogins:
        """Logins no person could be found for, newest first, at most 200. Org admins only."""
        return _parse(self._send("GET", "/v1/unlinked-logins"), UnlinkedLogins)

    def link_unlinked_login(self, unlinked_login_id: str, user_id: str) -> Linked:
        """Tie the login to an active person; their next login with it signs them in."""
        path = f"/v1/unlinked-logins/{_seg(unlinked_login_id)}/link"
        return _parse(self._send("POST", path, body=LinkIn(user_id=user_id)), Linked)

    def export_audit(
        self, fmt: str, *, since: str | None = None, until: str | None = None
    ) -> tuple[bytes, str]:
        """Every matching audit event, oldest first, and the file name the API suggests. Org
        admins in their own session only; the export is itself audited (``audit.exported``)."""
        query = {"format": fmt} | {k: v for k, v in (("since", since), ("until", until)) if v}
        r = self._send("GET", f"/v1/audit/export?{urlencode(query)}")
        found = re.search(r'filename="([^"/\\]+)"', r.headers.get("Content-Disposition", ""))
        return r.content, found[1] if found else f"audit.{fmt}"

    def find_groups(self, name: str) -> GroupMatches:
        return _parse(self._send("GET", f"/v1/groups?{urlencode({'name': name})}"), GroupMatches)

    def pull_kill_switch(self, app_id: str, mode: str) -> KillSwitchAccepted:
        """Stop the app now. Org admins only."""
        path = f"/v1/apps/{_seg(app_id)}/kill-switch"
        return _parse(
            self._send("POST", path, body=KillSwitchCreate(mode=mode)), KillSwitchAccepted
        )

    def get_kill_switch_run(self, app_id: str, run_id: str) -> KillSwitchRun:
        path = f"/v1/apps/{_seg(app_id)}/kill-switch/{_seg(run_id)}"
        return _parse(self._send("GET", path), KillSwitchRun)

    def enable_app(self, app_id: str) -> AppOut:
        """Make a stopped app active again. Org admins only."""
        return _parse(self._send("POST", f"/v1/apps/{_seg(app_id)}/enable"), AppOut)

    def explain_access(
        self, app_id: str, environment_id: str, user_id: str | None
    ) -> AccessExplained:
        """Why ``user_id`` (the caller when ``None``) can or cannot open the environment."""
        query = "" if user_id is None else f"?{urlencode({'user_id': user_id})}"
        path = f"{_environment_path(app_id, environment_id)}/access{query}"
        return _parse(self._send("GET", path), AccessExplained)

    def list_secrets(self, app_id: str, environment_id: str) -> SecretList:
        """Names and versions; there is no way to read a value back."""
        path = f"{_environment_path(app_id, environment_id)}/secrets"
        return _parse(self._send("GET", path), SecretList)

    def get_database(self, app_id: str, environment_id: str) -> DatabaseOut:
        path = f"{_environment_path(app_id, environment_id)}/database"
        return _parse(self._send("GET", path), DatabaseOut)

    def rotate_database(self, app_id: str, environment_id: str) -> DatabaseRotateOut:
        """Give the environment's database login a new password; a deployment starts when one
        is live. The password is never sent back."""
        path = f"{_environment_path(app_id, environment_id)}/database/rotate"
        return _parse(self._send("POST", path), DatabaseRotateOut)

    def get_logs(  # noqa: PLR0913  (keyword-only query)
        self,
        app_id: str,
        environment_id: str,
        *,
        source: str,
        since: int | None = None,
        after: str | None = None,
        wait: int = 0,
    ) -> LogPageOut:
        """The newest lines of the last ``since`` seconds, or with ``after`` the lines after that
        cursor, waiting up to ``wait`` seconds for one."""
        query: dict[str, str | int] = {"source": source}
        if after is None:
            query["since"] = since or 3600
        else:
            query |= {"after": after, "wait": wait}
        path = f"{_environment_path(app_id, environment_id)}/logs?{urlencode(query)}"
        return _parse(self._send("GET", path), LogPageOut)

    def get_health(self, app_id: str, environment_id: str) -> HealthOut:
        path = f"{_environment_path(app_id, environment_id)}/health"
        return _parse(self._send("GET", path), HealthOut)

    def get_usage(self, app_id: str, environment_id: str) -> UsageOut:
        path = f"{_environment_path(app_id, environment_id)}/usage"
        return _parse(self._send("GET", path), UsageOut)

    def grant_secret_upload(self, app_id: str, environment_id: str, name: str) -> SecretGrantOut:
        path = f"{_secret_path(app_id, environment_id, name)}/grants"
        return _parse(self._send("POST", path), SecretGrantOut)

    def upload_secret(self, target: UploadTarget, value: bytes) -> str:
        """PUT ``value`` to the cell's secret intake with the grant's headers and no API token;
        the version it added. Not retried: run the command again for a fresh grant."""
        try:
            r = self._upload_client().request(
                target.method,
                target.url,
                content=value,
                headers={**target.headers, "User-Agent": USER_AGENT},
            )
        except httpx2.TransportError as e:
            raise local_error(
                NETWORK_ERROR,
                "The secret could not be sent.",
                f"{type(e).__name__} reaching the cell's secret intake. Nothing was recorded; "
                "check your connection, then run the command again.",
                ExitCode.NETWORK,
            ) from None
        version = _intake_version(r)
        if version is None:
            raise local_error(
                UPLOAD_FAILED,
                "The secret was not stored.",
                f"The cell's secret intake answered HTTP {r.status_code}"
                f"{_intake_code(r)}. Run the command again; it asks for a fresh grant.",
            )
        return version

    def set_secret(self, app_id: str, environment_id: str, name: str, version: str) -> SecretSetOut:
        """Record the version the intake added; a deployment starts when one is live."""
        path = _secret_path(app_id, environment_id, name)
        return _parse(self._send("PUT", path, body=SecretSet(version=version)), SecretSetOut)

    # ── transport ────────────────────────────────────────────────────────────

    def get_json(self, path: str) -> dict[str, Any]:
        """The JSON object at ``path``, exactly as the API sent it."""
        r = self._send("GET", path)
        data = _json(r)
        if not isinstance(data, dict):
            raise local_error(
                BAD_RESPONSE,
                "The API answered in a shape this ssc does not understand.",
                f"GET {path} did not answer with a JSON object.",
            )
        return cast("dict[str, Any]", data)

    def post_json(self, path: str, body: Mapping[str, Any], key: str) -> httpx2.Response:
        """POST ``body`` with this ``Idempotency-Key``; sending the same key again replays."""
        return self._send("POST", path, body=body, headers={IDEMPOTENCY_HEADER: key})

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: BaseModel | Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        sent = dict(headers or {})
        if method == "POST":
            sent.setdefault(IDEMPOTENCY_HEADER, str(uuid.uuid4()))
        retryable = method in {"GET", "POST"}
        if isinstance(body, BaseModel):
            content = body.model_dump_json().encode()
        else:
            content = None if body is None else json.dumps(dict(body)).encode()
        if content is not None:
            sent["Content-Type"] = "application/json"
        retries = 0
        rate_waited = 0.0
        while True:
            try:
                token = self._token if isinstance(self._token, str) else self._token()
                sent["Authorization"] = f"Bearer {token}"
                r = self._http.request(method, path, content=content, headers=sent)
            except httpx2.TransportError as e:
                if retryable and retries < len(BACKOFF):
                    self._sleep(BACKOFF[retries])
                    retries += 1
                    continue
                raise local_error(
                    NETWORK_ERROR,
                    "The API could not be reached.",
                    f"{type(e).__name__} talking to {self.api_url}. "
                    "Check the address and your connection, then retry.",
                    ExitCode.NETWORK,
                ) from e
            if r.status_code == 429 and rate_waited < RATE_WAIT_SECONDS:
                wait = _retry_after(r)
                rate_waited += wait
                self._sleep(wait)
                continue
            if retryable and retries < len(BACKOFF) and _transient(r, method):
                self._sleep(BACKOFF[retries])
                retries += 1
                continue
            if r.status_code >= 400:
                raise _refusal(r)
            return r


def _environment_path(app_id: str, environment_id: str) -> str:
    return f"/v1/apps/{_seg(app_id)}/environments/{_seg(environment_id)}"


def _secret_path(app_id: str, environment_id: str, name: str) -> str:
    return f"{_environment_path(app_id, environment_id)}/secrets/{_seg(name)}"


def _intake_version(r: httpx2.Response) -> str | None:
    if r.status_code != 201:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    version = cast("dict[str, Any]", data).get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version.isdigit() else None


def _intake_code(r: httpx2.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return ""
    code = cast("dict[str, Any]", data).get("code") if isinstance(data, dict) else None
    return f" {code}" if isinstance(code, str) and code.isidentifier() else ""


def _grants_path(app_id: str, environment_id: str) -> str:
    return f"{_environment_path(app_id, environment_id)}/grants"


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as f:
        while chunk := f.read(UPLOAD_CHUNK):
            yield chunk


def _transient(r: httpx2.Response, method: str) -> bool:
    if r.status_code >= 500:
        return True
    return method == "POST" and r.status_code == 409 and _code(r) == ErrorCode.IDEMPOTENCY_IN_FLIGHT


def _retry_after(r: httpx2.Response) -> float:
    try:
        seconds = float(r.headers.get("Retry-After", "1"))
    except ValueError:
        seconds = 1.0
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def _json(r: httpx2.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return None


def _code(r: httpx2.Response) -> str | None:
    data = _json(r)
    if isinstance(data, dict):
        code = data.get("code")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return code if isinstance(code, str) else None
    return None


def _refusal(r: httpx2.Response) -> CliError:
    error = _problem(r)
    if r.status_code == 429:
        error.retry_after = _retry_after(r)
    return error


def _problem(r: httpx2.Response) -> CliError:
    data = _json(r)
    request_id = r.headers.get(REQUEST_ID_HEADER)
    if isinstance(data, dict):
        try:
            problem = ErrorBody.model_validate(
                {k: data.get(k) for k in ErrorBody.model_fields}  # pyright: ignore[reportUnknownMemberType]
                | {"status": r.status_code}
            )
        except ValidationError:
            pass
        else:
            if problem.request_id is None and request_id:
                problem = problem.model_copy(update={"request_id": request_id})
            return api_error(problem)
    return api_error(
        ErrorBody(
            code=BAD_RESPONSE,
            title="The API answered with an unexpected error.",
            detail=f"HTTP {r.status_code} without a problem body.",
            status=r.status_code,
            request_id=request_id,
        )
    )


def _parse[M: BaseModel](r: httpx2.Response, model: type[M]) -> M:
    try:
        return model.model_validate_json(r.content)
    except ValidationError as e:
        raise local_error(
            BAD_RESPONSE,
            "The API answered in a shape this ssc does not understand.",
            f"{model.__name__} did not validate. Upgrade ssc and retry.",
        ) from e
