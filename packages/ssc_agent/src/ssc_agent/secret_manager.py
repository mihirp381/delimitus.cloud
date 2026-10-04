"""App secrets in the cell's Secret Manager (SSC-026, decision 022).

Two narrow seams, neither with a read:

- ``SecretCustody.ensure``, in the cell agent: create the secret ``ssc-a-<env>-<NAME>`` in the
  cell's region if it is missing, make sure the environment's own service account exists, and
  set the secret's policy so that account alone may read it, which is how Cloud Run mounts it.
  ``SecretCustody.remove`` deletes one, for an app database the agent drops (SSC-042).
  A customer connection's credentials (SSC-051) are ``ssc-conn-<20>`` instead: created with the
  cell's connection tag, and readable by the data gateway's account alone.
- ``SecretWriter.add_version``, in the secret intake: add a version and return its number. The
  cell agent uses it too, for the app database secrets it makes itself (SSC-040).

Neither has a method that reads a value, and ``test_secrets`` fails if one is added. The cell's
deny rule refuses ``secretmanager.versions.access`` to every SSC service identity regardless, but
for the data gateway's on a secret with the connection tag.
"""

import asyncio
import base64
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

import httpx2

from ssc_agent.cloud_run import AccessTokens, CellRuntime
from ssc_shared.redaction import redact
from ssc_shared.runtime import CONNECTION_SECRET_ID, SECRET_ID, SECRET_VERSION
from ssc_shared.secret_grants import MAX_VALUE_BYTES

MANAGER_API: Final = "https://secretmanager.googleapis.com/v1"
ACCESSOR_ROLE: Final = "roles/secretmanager.secretAccessor"
CALL_TIMEOUT_SECONDS: Final = 30.0
POLICY_TRIES: Final = 6
_HTTP_BAD_REQUEST: Final = 400
_HTTP_NOT_FOUND: Final = 404
_HTTP_CONFLICT: Final = 409

type Identities = Callable[[str], Awaitable[None]]


class SecretsError(Exception):
    """Secret Manager refused or failed a call."""

    def __init__(self, what: str, status: int = 0, reason: str = "") -> None:
        super().__init__(f"{what}: HTTP {status} {reason}".strip() if status else what)
        self.status = status
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ConnectionSecrets:
    """Where connection secrets go (SSC-051): ``reader`` is the data gateway's service account,
    the only one granted on them, and ``tag_key`` and ``tag_value`` (``tagKeys/<n>``,
    ``tagValues/<n>``) the cell's connection tag, bound to each secret as it is created. The deny
    rule lets that account read a secret only while it carries the tag."""

    reader: str
    tag_key: str
    tag_value: str


class SecretCustody(Protocol):
    async def ensure(self, secret: str) -> None:
        """Create ``secret`` if missing and let only its environment's identity read it; for a
        connection secret, the data gateway's."""
        ...

    async def remove(self, secret: str) -> None:
        """Delete ``secret`` with every version; one already gone is fine."""
        ...


class SecretWriter(Protocol):
    async def add_version(self, secret: str, value: bytes) -> str:
        """Add ``value`` as the secret's next version and return the version's number."""
        ...


def service_of(secret: str) -> str:
    """The app service a secret id belongs to; ``ValueError`` for an id that is not an app's."""
    if SECRET_ID.fullmatch(secret) is None:
        raise ValueError(f"not an SSC app secret id: {secret!r}")
    return secret.rpartition("-")[0]


def check_secret(secret: str) -> None:
    """``ValueError`` unless ``secret`` is an app's secret id or a connection's (SSC-051)."""
    if SECRET_ID.fullmatch(secret) is None and CONNECTION_SECRET_ID.fullmatch(secret) is None:
        raise ValueError(f"not an SSC app or connection secret id: {secret!r}")


class _Api:
    def __init__(self, tokens: AccessTokens, client: httpx2.AsyncClient | None) -> None:
        self._tokens = tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def call(
        self,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        params: Mapping[str, str] | None = None,
        method: str = "POST",
    ) -> dict[str, Any]:
        what = f"{method} {path.rsplit('/', 1)[-1]}"
        headers = {"Authorization": f"Bearer {await self._tokens()}"}
        try:
            response = await self._client.request(
                method,
                f"{MANAGER_API}/{path}",
                json=None if json is None else dict(json),
                params=dict(params or {}),
                headers=headers,
            )
        except httpx2.HTTPError as exc:
            raise SecretsError(f"{what}: {type(exc).__name__}") from None
        if response.status_code >= _HTTP_BAD_REQUEST:
            raise SecretsError(what, response.status_code, _reason(response))
        if not response.content:
            return {}
        payload: object = response.json()
        return cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}

    async def aclose(self) -> None:
        await self._client.aclose()


class CellSecretCustody(SecretCustody):
    """``SecretCustody`` as the cell agent, whose ``secretmanager.admin`` is limited to
    ``ssc-a-*`` and ``ssc-conn-*``; the id check here is what limits the create. Without
    ``connections`` it refuses connection secrets."""

    def __init__(  # noqa: PLR0913  (keyword-only)
        self,
        cell: CellRuntime,
        tokens: AccessTokens,
        identities: Identities,
        *,
        client: httpx2.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        connections: ConnectionSecrets | None = None,
    ) -> None:
        self._cell = cell
        self._api = _Api(tokens, client)
        self._identities = identities
        self._sleep = sleep
        self._connections = connections

    async def ensure(self, secret: str) -> None:
        if CONNECTION_SECRET_ID.fullmatch(secret) is not None:
            await self._ensure_connection(secret)
            return
        service = service_of(secret)
        await self._create(secret, {"labels": {"ssc-service": service}})
        await self._identities(service)
        await self._grant(secret, self._cell.identity(service))

    async def _ensure_connection(self, secret: str) -> None:
        """Tagged at creation, so the secret never exists without the tag; one made earlier
        without it stays unreadable to the data gateway."""
        connections = self._connections
        if connections is None:
            raise SecretsError(f"{secret}: this agent keeps no connection secrets")
        await self._create(secret, {"tags": {connections.tag_key: connections.tag_value}})
        await self._grant(secret, connections.reader)

    async def _create(self, secret: str, fields: Mapping[str, object]) -> None:
        replication = {"userManaged": {"replicas": [{"location": self._cell.region}]}}
        try:
            await self._api.call(
                f"projects/{self._cell.project}/secrets",
                json={"replication": replication, **fields},
                params={"secretId": secret},
            )
        except SecretsError as exc:
            if exc.status != _HTTP_CONFLICT:
                raise

    async def _grant(self, secret: str, account: str) -> None:
        """``account`` alone may read ``secret``: the policy is replaced, not merged."""
        member = f"serviceAccount:{account}"
        policy = {"bindings": [{"role": ACCESSOR_ROLE, "members": [member]}]}
        path = f"projects/{self._cell.project}/secrets/{secret}:setIamPolicy"
        for attempt in range(POLICY_TRIES):
            try:
                await self._api.call(path, json={"policy": policy})
            except SecretsError as exc:
                if exc.status == _HTTP_BAD_REQUEST and "service account" in exc.reason.lower():
                    await self._sleep(min(2.0**attempt, 10.0))
                    continue
                raise
            return
        raise SecretsError(f"{secret}: its service account is still not usable")

    async def remove(self, secret: str) -> None:
        service_of(secret)
        try:
            await self._api.call(f"projects/{self._cell.project}/secrets/{secret}", method="DELETE")
        except SecretsError as exc:
            if exc.status != _HTTP_NOT_FOUND:
                raise

    async def aclose(self) -> None:
        await self._api.aclose()


class CellSecretWriter(SecretWriter):
    """``SecretWriter`` as the secret intake, which holds ``secretVersionAdder`` on ``ssc-a-*``
    and ``ssc-conn-*`` and nothing else."""

    def __init__(
        self, project: str, tokens: AccessTokens, *, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._project = project
        self._api = _Api(tokens, client)

    async def add_version(self, secret: str, value: bytes) -> str:
        check_secret(secret)
        if not 0 < len(value) <= MAX_VALUE_BYTES:
            raise ValueError(f"a secret value is 1 to {MAX_VALUE_BYTES} bytes")
        body = await self._api.call(
            f"projects/{self._project}/secrets/{secret}:addVersion",
            json={"payload": {"data": base64.b64encode(value).decode("ascii")}},
        )
        version = str(body.get("name") or "").rsplit("/", 1)[-1]
        if SECRET_VERSION.fullmatch(version) is None:
            raise SecretsError(f"{secret}: addVersion returned no version number")
        return version

    async def aclose(self) -> None:
        await self._api.aclose()


def _reason(response: httpx2.Response) -> str:
    try:
        payload: object = response.json()
    except ValueError:
        return response.reason_phrase
    found = cast("dict[str, Any]", payload).get("error") if isinstance(payload, dict) else None
    error = cast("dict[str, Any]", found) if isinstance(found, dict) else {}
    return redact(f"{error.get('status', '')} {error.get('message', '')}".strip())
