"""HTTP client for ``/v1`` over httpx2.

* GET and POST are retried up to three times with backoff on connection errors and 5xx. Every
  POST carries one ``Idempotency-Key`` per logical operation, reused on each retry, so a retry
  can never do the work twice. A keyed POST also waits out ``IDEMPOTENCY_IN_FLIGHT``.
* PUT is conditional (``If-Match``) and is not retried here; callers handle ``412`` themselves.
  ``PUT .../grants`` answers ``202`` with the pending approval ids when the change needs approval.
* ``429`` is retried once after ``Retry-After``: the API refuses before doing any work.
* A refusal becomes a :class:`~ssc_cli.errors.CliError` carrying the API's problem members.
* Every request names the tool in ``X-SSC-Source-Tool`` for the API's source tool mix.
"""

import time
import uuid
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, Final, Self
from urllib.parse import quote

import httpx2
from pydantic import BaseModel, ValidationError

from ssc_cli import __version__
from ssc_cli.errors import (
    BAD_RESPONSE,
    NETWORK_ERROR,
    CliError,
    ErrorBody,
    ExitCode,
    api_error,
    local_error,
)
from ssc_cli.models import (
    AppCreate,
    AppList,
    AppOut,
    GrantIn,
    GrantsIn,
    GrantsOut,
    GrantsPending,
    OperationOut,
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
DEFAULT_TIMEOUT: Final = 30.0

Sleep = Callable[[float], None]


def _seg(value: str) -> str:
    return quote(value, safe="")


class ApiClient:
    """One API address and one token. Use as a context manager."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx2.BaseTransport | None = None,
        sleep: Sleep = time.sleep,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_url = base_url
        self._sleep = sleep
        self._http = httpx2.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {token}",
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
        self._http.close()

    # ── endpoints ────────────────────────────────────────────────────────────

    def whoami(self) -> Whoami:
        return _parse(self._send("GET", "/v1/whoami"), Whoami)

    def list_apps(self) -> AppList:
        return _parse(self._send("GET", "/v1/apps"), AppList)

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

    # ── transport ────────────────────────────────────────────────────────────

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: BaseModel | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        sent = dict(headers or {})
        if method == "POST":
            sent[IDEMPOTENCY_HEADER] = str(uuid.uuid4())
        retryable = method in {"GET", "POST"}
        content = body.model_dump_json().encode() if body is not None else None
        if content is not None:
            sent["Content-Type"] = "application/json"
        retries = 0
        waited_for_rate = False
        while True:
            try:
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
            if r.status_code == 429 and not waited_for_rate:
                waited_for_rate = True
                self._sleep(_retry_after(r))
                continue
            if retryable and retries < len(BACKOFF) and _transient(r, method):
                self._sleep(BACKOFF[retries])
                retries += 1
                continue
            if r.status_code >= 400:
                raise _refusal(r)
            return r


def _grants_path(app_id: str, environment_id: str) -> str:
    return f"/v1/apps/{_seg(app_id)}/environments/{_seg(environment_id)}/grants"


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
