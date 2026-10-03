"""App logs and health through the cell agent (SSC-024).

The agent reads the cell's Cloud Logging, builds the filter from the service name itself, and
keeps the cell under the quota; this client only names the service and who asks. A follow waits
in the agent for up to ``MAX_WAIT_SECONDS``, so the timeout covers that plus the read.
"""

from typing import Any, Final, cast

import httpx2

from ssc_control.runtime.cell_agent import IdTokens
from ssc_shared.logs import (
    CellLogs,
    Health,
    LogPage,
    LogQuery,
    LogsError,
    LogsNotConfiguredError,
    LogsRateLimitedError,
    health_from_wire,
    page_from_wire,
    query_to_wire,
)
from ssc_shared.redaction import redact

CALL_TIMEOUT_SECONDS: Final = 60.0
_HTTP_TOO_MANY: Final = 429
_HTTP_UNAVAILABLE: Final = 503


class AgentCellLogs(CellLogs):
    """``CellLogs`` through one cell's agent, with an ID token for its URL on every call."""

    def __init__(
        self, agent_url: str, id_tokens: IdTokens, *, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._url = agent_url.rstrip("/")
        self._id_tokens = id_tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def read(
        self, query: LogQuery, *, since_seconds: int, limit: int, caller: str
    ) -> LogPage:
        body = await self._call(
            "read",
            {
                "query": query_to_wire(query),
                "since_seconds": since_seconds,
                "limit": limit,
                "caller": caller,
            },
        )
        return _page(body)

    async def follow(
        self, query: LogQuery, *, cursor: str | None, wait_seconds: float, caller: str
    ) -> LogPage:
        body = await self._call(
            "follow",
            {
                "query": query_to_wire(query),
                "cursor": cursor,
                "wait_seconds": int(wait_seconds),
                "caller": caller,
            },
        )
        return _page(body)

    async def health(self, service: str, *, caller: str) -> Health:
        body = await self._call("health", {"service": service, "caller": caller})
        try:
            return health_from_wire(body)
        except ValueError as exc:
            raise LogsError(f"cell agent health: {exc}") from None

    async def _call(self, method: str, payload: dict[str, object]) -> dict[str, Any]:
        token = await self._id_tokens(self._url)
        try:
            response = await self._client.post(
                f"{self._url}/v1/logs/{method}",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx2.HTTPError as exc:
            raise LogsError(f"cell agent {method}: {type(exc).__name__}") from None
        try:
            answer: object = response.json()
        except ValueError:
            answer = None
        body = cast("dict[str, Any]", answer) if isinstance(answer, dict) else {}
        if response.is_success:
            return body
        code = str(body.get("code", ""))
        message = redact(f"cell agent {method}: HTTP {response.status_code} {code}")
        if response.status_code == _HTTP_TOO_MANY:
            retry: object = body.get("retry_after")
            seconds = retry if isinstance(retry, int) and not isinstance(retry, bool) else 30
            raise LogsRateLimitedError(message, seconds)
        if response.status_code == _HTTP_UNAVAILABLE and code == "LOGS_NOT_CONFIGURED":
            raise LogsNotConfiguredError(message)
        raise LogsError(message)

    async def aclose(self) -> None:
        await self._client.aclose()


def _page(body: dict[str, Any]) -> LogPage:
    try:
        return page_from_wire(body)
    except ValueError as exc:
        raise LogsError(f"cell agent logs: {exc}") from None
