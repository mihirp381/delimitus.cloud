"""``AdminSql`` on the cell's Cloud SQL instance, over the Admin API v1 (SSC-040, decision 003).

Statements go through ``instances.executeSql`` as the agent's own IAM database user
(``autoIamAuthn``), a member of ``cloudsqlsuperuser``, so the agent needs no password and no
network path to the instance. The Data API runs a batch in one transaction, for at most 30
seconds, and reports a failed statement as HTTP 200 with ``status.code`` set, so the status is
checked as well as the HTTP code. ``CREATE DATABASE`` and ``DROP DATABASE`` cannot run in a
transaction: ``databases.insert`` makes the database and ``databases.delete`` drops it. Apps
connect to the instance's DNS name when it has one, else to its private address, and verify it
against every CA ``listServerCas`` returns.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final, cast

import httpx2

from ssc_agent.app_database import AdminSqlError, Rows
from ssc_agent.cloud_run import AccessTokens
from ssc_shared.redaction import redact

SQL_API: Final = "https://sqladmin.googleapis.com/v1"
POSTGRES_PORT: Final = 5432
CALL_TIMEOUT_SECONDS: Final = 45.0
OPERATION_POLL_SECONDS: Final = 0.5
OPERATION_TIMEOUT_SECONDS: Final = 120.0
_HTTP_BAD_REQUEST: Final = 400
_HTTP_NOT_FOUND: Final = 404
_HTTP_CONFLICT: Final = 409
_EXISTS: Final = "already exists"


class CloudSqlAdmin:
    """The instance ``instance`` of the cell project ``project``."""

    def __init__(
        self,
        project: str,
        instance: str,
        tokens: AccessTokens,
        *,
        client: httpx2.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base = f"{SQL_API}/projects/{project}"
        self._instance = f"{self._base}/instances/{instance}"
        self._tokens = tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)
        self._sleep = sleep

    async def run(self, database: str, statements: Sequence[str]) -> Rows:
        body = {
            "sqlStatement": ";\n".join(statements),
            "database": database,
            "autoIamAuthn": True,
            "partialResultMode": "FAIL_PARTIAL_RESULT",
            "application": "ssc-cell-agent",
        }
        payload = await self._call("POST", f"{self._instance}/executeSql", body)
        status = _obj(payload.get("status"))
        if status.get("code"):
            raise AdminSqlError(redact(f"executeSql: {status.get('message', '')}"))
        results = _objs(payload.get("results"))
        if not results:
            return []
        last = results[-1]
        names = [str(c.get("name")) for c in _objs(last.get("columns"))]
        return [
            {
                name: None if cell.get("nullValue") else cell.get("value")
                for name, cell in zip(names, _objs(row.get("values")), strict=True)
            }
            for row in _objs(last.get("rows"))
        ]

    async def create_database(self, name: str) -> None:
        try:
            operation = await self._call("POST", f"{self._instance}/databases", {"name": name})
            await self._wait("databases.insert", str(operation.get("name") or ""))
        except AdminSqlError as exc:
            if exc.status != _HTTP_CONFLICT and _EXISTS not in str(exc):
                raise

    async def drop_database(self, name: str) -> None:
        try:
            operation = await self._call("DELETE", f"{self._instance}/databases/{name}")
        except AdminSqlError as exc:
            if exc.status != _HTTP_NOT_FOUND:
                raise
            return
        await self._wait("databases.delete", str(operation.get("name") or ""))

    async def endpoint(self) -> tuple[str, int]:
        instance = await self._call("GET", self._instance)
        if dns := str(instance.get("dnsName") or ""):
            return dns.rstrip("."), POSTGRES_PORT
        for address in _objs(instance.get("ipAddresses")):
            if address.get("type") == "PRIVATE" and address.get("ipAddress"):
                return str(address["ipAddress"]), POSTGRES_PORT
        raise AdminSqlError("the instance has no private address")

    async def server_ca(self) -> str:
        listed = await self._call("GET", f"{self._instance}/listServerCas")
        pems = [str(c["cert"]).strip() for c in _objs(listed.get("certs")) if c.get("cert")]
        if not pems:
            raise AdminSqlError("the instance lists no server CA")
        return "\n".join(pems) + "\n"

    async def _wait(self, what: str, operation: str) -> None:
        if not operation:
            raise AdminSqlError(f"{what} returned no operation")
        waited = 0.0
        while waited < OPERATION_TIMEOUT_SECONDS:
            op = await self._call("GET", f"{self._base}/operations/{operation}")
            if op.get("status") == "DONE":
                if errors := _objs(_obj(op.get("error")).get("errors")):
                    raise AdminSqlError(redact(f"{what}: {errors[0].get('message')}"))
                return
            await self._sleep(OPERATION_POLL_SECONDS)
            waited += OPERATION_POLL_SECONDS
        raise AdminSqlError(f"{what} did not finish in time")

    async def _call(self, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
        what = f"{method} {url.rsplit('/', 1)[-1]}"
        headers = {"Authorization": f"Bearer {await self._tokens()}"}
        try:
            response = await self._client.request(method, url, json=body, headers=headers)
        except httpx2.HTTPError as exc:
            raise AdminSqlError(f"{what}: {type(exc).__name__}") from None
        if response.status_code >= _HTTP_BAD_REQUEST:
            raise AdminSqlError(
                f"{what}: HTTP {response.status_code} {_reason(response)}", response.status_code
            )
        payload: object = response.json() if response.content else {}
        return cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}

    async def aclose(self) -> None:
        await self._client.aclose()


def _obj(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _objs(value: object) -> list[dict[str, Any]]:
    return [_obj(v) for v in cast("list[object]", value)] if isinstance(value, list) else []


def _reason(response: httpx2.Response) -> str:
    try:
        payload: object = response.json()
    except ValueError:
        return response.reason_phrase
    error = _obj(_obj(payload).get("error"))
    return redact(f"{error.get('status', '')} {error.get('message', '')}".strip())
