"""What only the cell can remove of a deleted app's environment (GA-12): its files and secrets.

Removing an app deletes each environment's files in the cell bucket and each of its secrets with
every version. The cell agent does both, and only for an app service of its own org that is gone
or stopped (``SERVICE_LIVE`` otherwise). The control plane names the service or the secret and
gets back a count or nothing; no call here reads, returns or carries a value. The service itself
goes through ``RuntimeDriver.remove`` and its database through ``AppDatabases.drop``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, cast

import httpx2

from ssc_control.runtime.cell_agent import IdTokens
from ssc_shared.redaction import redact
from ssc_shared.runtime import ORG_HEADER, check_org

CALL_TIMEOUT_SECONDS: Final = 120.0
NOT_CONFIGURED: Final = frozenset({"FILES_NOT_CONFIGURED", "SECRETS_NOT_CONFIGURED"})
"""The agent's answers that mean its cell has no bucket, or keeps no secrets, at all."""


class CellRemovalError(Exception):
    """The cell could not delete the files or the secret."""

    def __init__(self, message: str) -> None:
        super().__init__(redact(message))


class CellRemoval(Protocol):
    async def drop_files(self, service: str) -> int:
        """Delete every file the service's environment kept; how many. None left is fine."""
        ...

    async def remove_secret(self, secret: str) -> bool:
        """Delete an app's secret with every version; one already gone is fine. False when the
        cell keeps no secrets at all."""
        ...


class AgentCellRemoval(CellRemoval):
    """``CellRemoval`` through one cell's agent, with an ID token for its URL on every call."""

    def __init__(
        self,
        agent_url: str,
        id_tokens: IdTokens,
        *,
        org_id: str,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._url = agent_url.rstrip("/")
        self._id_tokens = id_tokens
        self._org = check_org(org_id)
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def drop_files(self, service: str) -> int:
        body = await self._call("files/drop", {"service": service})
        if body is None:
            return 0
        files = body.get("files")
        if not isinstance(files, int) or isinstance(files, bool) or files < 0:
            raise CellRemovalError("cell agent files/drop: files is not a count")
        return files

    async def remove_secret(self, secret: str) -> bool:
        return await self._call("secrets/remove", {"secret": secret}) is not None

    async def _call(self, method: str, payload: Mapping[str, object]) -> dict[str, Any] | None:
        """The agent's answer, or None when its cell has nothing of that kind."""
        token = await self._id_tokens(self._url)
        try:
            response = await self._client.post(
                f"{self._url}/v1/{method}",
                json=dict(payload),
                headers={"Authorization": f"Bearer {token}", ORG_HEADER: self._org},
            )
        except httpx2.HTTPError as exc:
            raise CellRemovalError(f"cell agent {method}: {type(exc).__name__}") from None
        try:
            parsed: object = response.json()
        except ValueError:
            parsed = None
        body = cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else {}
        if response.is_success:
            return body
        code = str(body.get("code", ""))
        if code in NOT_CONFIGURED:
            return None
        message = f"{code} {body.get('message', response.reason_phrase)}"
        raise CellRemovalError(f"cell agent {method}: HTTP {response.status_code} {message}")

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass(slots=True)
class FakeCellRemoval(CellRemoval):
    """In memory, for tests and local development: ``files`` is how many each service kept."""

    files: dict[str, int] = field(default_factory=dict[str, int])
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    async def drop_files(self, service: str) -> int:
        self.calls.append(("drop_files", service))
        return self.files.pop(service, 0)

    async def remove_secret(self, secret: str) -> bool:
        self.calls.append(("remove_secret", secret))
        return True


__all__ = [
    "AgentCellRemoval",
    "CellRemoval",
    "CellRemovalError",
    "FakeCellRemoval",
]
