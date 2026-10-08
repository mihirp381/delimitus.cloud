"""The cell's data gateway, asked on the control plane's behalf (GA-5.8).

``schema`` asks the gateway which tables and columns one connection shows one environment: a
``GET {SSC_DATAGW_URL}/v1/connections/{name}/schema`` with the agent's own Google ID token for
the gateway's audience (``SSC_DATAGW_AUDIENCE``) and the environment in ``X-SSC-Environment``.
The gateway admits the agent's account on that route alone (``SSC_DATAGW_AGENT_ACCOUNT``) and
answers as if the environment's app had asked. The agent hands back the gateway's status and
JSON as they are, so the control plane maps the gateway's codes itself. One try, 30 s.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

import httpx2

from ssc_agent.metadata import MetadataError

CALL_TIMEOUT_SECONDS: Final = 30.0
ENVIRONMENT_HEADER: Final = "X-SSC-Environment"
CONNECTION_NAME: Final = re.compile(r"[a-z][a-z0-9-]{0,62}")
ENVIRONMENT_ID: Final = re.compile(r"env_[a-z0-9]{20}")

type IdTokens = Callable[[], Awaitable[str]]


class DataGatewayError(RuntimeError):
    """The gateway could not be asked: no ID token, no answer in time, or a lost connection."""


@dataclass(frozen=True, slots=True)
class GatewayAnswer:
    status: int
    body: object


class DataGateway:
    """The cell's data gateway at ``url``, called with ``id_tokens`` for its audience."""

    def __init__(
        self, url: str, id_tokens: IdTokens, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._url = url.rstrip("/")
        self._id_tokens = id_tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def schema(self, connection: str, environment_id: str) -> GatewayAnswer:
        """The gateway's answer for ``connection`` as ``environment_id`` sees it; ``ValueError``
        for a name or an id that is not one."""
        if CONNECTION_NAME.fullmatch(connection) is None:
            raise ValueError("connection is not a connection name")
        if ENVIRONMENT_ID.fullmatch(environment_id) is None:
            raise ValueError("environment_id is not an environment id")
        try:
            token = await self._id_tokens()
        except MetadataError as exc:
            raise DataGatewayError(str(exc)) from None
        try:
            response = await self._client.get(
                f"{self._url}/v1/connections/{connection}/schema",
                headers={"Authorization": f"Bearer {token}", ENVIRONMENT_HEADER: environment_id},
                timeout=CALL_TIMEOUT_SECONDS,
            )
        except httpx2.HTTPError as exc:
            raise DataGatewayError(f"data gateway: {type(exc).__name__}") from None
        try:
            body: object = response.json()
        except ValueError:
            body = None
        return GatewayAnswer(status=response.status_code, body=body)
