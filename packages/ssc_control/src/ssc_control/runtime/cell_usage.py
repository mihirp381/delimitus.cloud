"""App usage through the cell agent (SSC-028).

The agent reads the cell's Cloud Monitoring for every app service at once; this client only
names the window. It never reaches an app.
"""

from typing import Any, Final, cast

import httpx2

from ssc_control.runtime.cell_agent import IdTokens
from ssc_shared.redaction import redact
from ssc_shared.usage import (
    CellUsage,
    UsageError,
    UsageNotConfiguredError,
    UsageReport,
    UsageWindow,
    report_from_wire,
    window_to_wire,
)

CALL_TIMEOUT_SECONDS: Final = 120.0
_HTTP_UNAVAILABLE: Final = 503


class AgentCellUsage(CellUsage):
    """``CellUsage`` through one cell's agent, with an ID token for its URL on every call."""

    def __init__(
        self, agent_url: str, id_tokens: IdTokens, *, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._url = agent_url.rstrip("/")
        self._id_tokens = id_tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def read(self, window: UsageWindow) -> UsageReport:
        token = await self._id_tokens(self._url)
        try:
            response = await self._client.post(
                f"{self._url}/v1/usage/read",
                json={"window": window_to_wire(window)},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx2.HTTPError as exc:
            raise UsageError(f"cell agent usage: {type(exc).__name__}") from None
        try:
            answer: object = response.json()
        except ValueError:
            answer = None
        body = cast("dict[str, Any]", answer) if isinstance(answer, dict) else {}
        if not response.is_success:
            code = str(body.get("code", ""))
            message = redact(f"cell agent usage: HTTP {response.status_code} {code}")
            if response.status_code == _HTTP_UNAVAILABLE and code == "USAGE_NOT_CONFIGURED":
                raise UsageNotConfiguredError(message)
            raise UsageError(message)
        try:
            return report_from_wire(body)
        except ValueError as exc:
            raise UsageError(f"cell agent usage: {exc}") from None

    async def aclose(self) -> None:
        await self._client.aclose()
