"""Write grants for the cell's secret intake (SSC-026, decision 022).

The control plane never holds a secret value. For ``ssc secret set`` it asks the cell agent to
make sure the secret exists and that only the environment's identity may read it, then mints a
grant (``ssc_shared.secret_grants``): a fresh Google ID token of its own service account whose
audience is the one upload URL. The command line PUTs the value there itself.

``SecretGrants`` has one method and it returns no value; there is nothing here to read one with.
"""

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast

import httpx2

from ssc_control.runtime.cell_agent import IdTokens
from ssc_shared.redaction import redact
from ssc_shared.secret_grants import GRANT_SECONDS, upload_url

CALL_TIMEOUT_SECONDS: Final = 60.0


class SecretGrantError(Exception):
    """The cell could not prepare the secret; nothing was granted."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SecretGrant:
    """Where to PUT one secret's next value and the bearer grant that allows it, until
    ``expires_at``. A credential: never logged, never stored beyond the reply."""

    url: str
    token: str
    expires_at: datetime


class SecretGrants(Protocol):
    async def grant(self, secret: str) -> SecretGrant:
        """Prepare ``secret`` in the cell and grant one upload of its next version."""
        ...


class CellSecretGrants(SecretGrants):
    """Grants through one cell: its agent at ``agent_url``, its intake at ``intake_origin``.
    ``agent_tokens`` are ID tokens for the agent; ``grant_tokens`` must mint a fresh token on
    every call, since each audience is used once."""

    def __init__(  # noqa: PLR0913  (keyword-only)
        self,
        *,
        agent_url: str,
        intake_origin: str,
        agent_tokens: IdTokens,
        grant_tokens: IdTokens,
        client: httpx2.AsyncClient | None = None,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._agent = agent_url.rstrip("/")
        self._origin = intake_origin.rstrip("/")
        self._agent_tokens = agent_tokens
        self._grant_tokens = grant_tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)
        self._wall = wall

    async def grant(self, secret: str) -> SecretGrant:
        await self._ensure(secret)
        url = upload_url(self._origin, secret, secrets.token_urlsafe(24))
        issued = self._wall()
        token = await self._grant_tokens(url)
        expires = datetime.fromtimestamp(issued, UTC) + timedelta(seconds=GRANT_SECONDS)
        return SecretGrant(url=url, token=token, expires_at=expires)

    async def _ensure(self, secret: str) -> None:
        token = await self._agent_tokens(self._agent)
        try:
            response = await self._client.post(
                f"{self._agent}/v1/secrets/ensure",
                json={"secret": secret},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx2.HTTPError as exc:
            raise SecretGrantError(f"cell agent ensure: {type(exc).__name__}") from None
        if response.is_success:
            return
        try:
            payload: object = response.json()
        except ValueError:
            payload = None
        body = cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}
        message = f"{body.get('code', '')} {body.get('message', response.reason_phrase)}"
        raise SecretGrantError(redact(f"cell agent ensure: HTTP {response.status_code} {message}"))

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["CellSecretGrants", "SecretGrant", "SecretGrantError", "SecretGrants"]
