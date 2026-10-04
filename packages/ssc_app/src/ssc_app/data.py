"""Read a database the org connected, through the cell's data gateway (SSC-052). Name the connection
in ``[connections] names`` in ``ssc.toml``; an admin grants the environment the connection.

    from ssc_app import data

    result = data.query("finance", "select id, total from invoices where year = $1", [2026])
    for row in result.rows:       # each row is a list in column order
        ...

The call carries the app's own workload token (``ssc_app.workload``), so the app holds no database
credentials. The statement is one read-only ``SELECT`` with ``$1``, ``$2``, ... placeholders;
``max_rows``, ``max_bytes`` and ``timeout_ms`` only narrow what the platform, the connection and the
grant allow, and ``truncated`` says the gateway stopped reading before the result ended. To act for
the signed-in user, pass the ``X-SSC-Identity`` value of the request being answered as
``identity``; without it the app acts for itself (``docs/contracts/data-gateway.md``).

The data gateway's address comes from the metadata server, as for ``ssc_app.files``, and
``SSC_DATAGW_URL`` or ``url=`` replaces it. A query only reads, so it is tried once more when the
data gateway cannot be reached or answers 502, 503 or 504; a query that timed out is not. Same
names and behaviour as the Node helper ``@delimitus/ssc-data``.
"""

import json
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ssc_app.files import RETRY_STATUSES, URL_VARIABLE, FilesError, gateway_url
from ssc_app.workload import METADATA, WorkloadToken

QUERY_TIMEOUT_SECONDS: Final = 60.0
"""Longer than the data gateway's 30 s query limit plus its start from zero."""
IDENTITY_HEADER: Final = "x-ssc-identity"


class DataError(RuntimeError):
    """A refusal or a failure. ``code`` is the data gateway's (``CONNECTION_NOT_GRANTED``,
    ``CONNECTION_SUSPENDED``, ``QUERY_REFUSED``, ``QUERY_FAILED``, ``DAILY_BUDGET_SPENT``, ...) or
    ``UNREACHABLE``; ``sqlstate`` is the database's for ``QUERY_FAILED`` when it gave one."""

    def __init__(
        self, code: str, message: str, status: int | None = None, sqlstate: str | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status = status
        self.sqlstate = sqlstate


@dataclass(frozen=True, slots=True)
class QueryResult:
    """What a query returned. ``columns`` are ``{name, type, db_type}``; a decimal is a string,
    a timestamp is ISO 8601, bytes are base64."""

    columns: list[dict[str, str]]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    truncated_reason: str | None
    request_id: str


def _send(url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method="POST", headers=dict(headers))  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=QUERY_TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _refusal(status: int, raw: bytes) -> DataError:
    try:
        error = json.loads(raw)["error"]
        return DataError(str(error["code"]), str(error["message"]), status, error.get("sqlstate"))
    except ValueError, KeyError, TypeError:
        return DataError("UNAVAILABLE", f"the data gateway answered HTTP {status}", status)


class Data:
    """The data gateway of the cell this app runs in. ``url`` and ``metadata`` replace the data
    gateway's address and the metadata server's (tests). Safe to share between threads."""

    def __init__(self, *, url: str | None = None, metadata: str = METADATA) -> None:
        self._url = (url or os.environ.get(URL_VARIABLE) or "").rstrip("/") or None
        self._metadata = metadata
        self._tokens: WorkloadToken | None = None
        self._lock = threading.Lock()

    def query(  # noqa: PLR0913  (keyword-only)
        self,
        name: str,
        sql: str,
        params: Sequence[Any] = (),
        *,
        max_rows: int | None = None,
        max_bytes: int | None = None,
        timeout_ms: int | None = None,
        identity: str | None = None,
    ) -> QueryResult:
        """Run one read-only statement on connection ``name``. ``DataError`` for a refusal."""
        asks = {"max_rows": max_rows, "max_bytes": max_bytes, "timeout_ms": timeout_ms}
        body: dict[str, Any] = {"sql": sql, "params": list(params)}
        body.update({k: v for k, v in asks.items() if v is not None})
        raw_body = json.dumps(body).encode()
        url, tokens = self._target()
        for attempt in (1, 2):
            headers = {
                "authorization": f"Bearer {tokens.get()}",
                "content-type": "application/json",
            }
            if identity is not None:
                headers[IDENTITY_HEADER] = identity
            try:
                status, raw = _send(f"{url}/v1/connections/{name}/query", headers, raw_body)
            except TimeoutError:
                raise DataError("UNREACHABLE", "data gateway: TimeoutError") from None
            except (urllib.error.URLError, OSError) as exc:
                if attempt == 1:
                    continue
                raise DataError("UNREACHABLE", f"data gateway: {type(exc).__name__}") from None
            if status in RETRY_STATUSES and attempt == 1:
                continue
            if status != 200:  # noqa: PLR2004
                raise _refusal(status, raw)
            answer = json.loads(raw)
            return QueryResult(
                columns=answer["columns"],
                rows=answer["rows"],
                row_count=answer["row_count"],
                truncated=answer["truncated"],
                truncated_reason=answer["truncated_reason"],
                request_id=answer["request_id"],
            )
        raise AssertionError

    def _target(self) -> tuple[str, WorkloadToken]:
        with self._lock:
            if self._url is None:
                try:
                    self._url = gateway_url(self._metadata)
                except FilesError as exc:
                    raise DataError(exc.code, str(exc).partition(": ")[2]) from None
            if self._tokens is None:
                self._tokens = WorkloadToken(audience=self._url, metadata=self._metadata)
            return self._url, self._tokens


_default: Data | None = None
_default_lock = threading.Lock()


def query(  # noqa: PLR0913  (keyword-only)
    name: str,
    sql: str,
    params: Sequence[Any] = (),
    *,
    max_rows: int | None = None,
    max_bytes: int | None = None,
    timeout_ms: int | None = None,
    identity: str | None = None,
) -> QueryResult:
    """``Data.query`` on this app's cell."""
    global _default  # noqa: PLW0603
    with _default_lock:
        if _default is None:
            _default = Data()
    return _default.query(
        name,
        sql,
        params,
        max_rows=max_rows,
        max_bytes=max_bytes,
        timeout_ms=timeout_ms,
        identity=identity,
    )
