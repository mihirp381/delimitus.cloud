"""A connection's tables and columns, from the cell's data gateway through the cell agent (GA-5.8).

The control plane cannot reach the data gateway: its ingress is the cell's VPC alone. The agent
asks it with its own ID token, naming the environment, and answers with the gateway's status and
JSON (``ssc_agent.datagw``). ``schema_of`` turns that into a ``Schema`` or a ``SchemaRefusal``
carrying the gateway's code; anything the agent itself could not do is ``CellSchemaError``.

``SchemaCache`` keeps each answer for :data:`CACHE_SECONDS` per (org, environment, connection),
in this process only; refusals are never kept.
"""

import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, cast

import httpx2

from ssc_control.runtime.cell_agent import IdTokens
from ssc_shared.redaction import redact
from ssc_shared.runtime import ORG_HEADER, check_org

CALL_TIMEOUT_SECONDS: Final = 40.0
"""The agent gives the gateway 30 s; this covers that and the agent's own work."""
CACHE_SECONDS: Final = 300.0
CACHE_ENTRIES: Final = 1000


class CellSchemaError(Exception):
    """The agent could not ask the gateway, or answered with something that is not an answer."""

    def __init__(self, message: str) -> None:
        super().__init__(redact(message))


class SchemaRefusal(Exception):  # noqa: N818  (a refusal, not a failure)
    """The gateway refused; ``code`` is its own, empty when it gave none."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"data gateway: HTTP {status} {code}".strip())
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str
    db_type: str | None


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    columns: tuple[Column, ...]


@dataclass(frozen=True, slots=True)
class Schema:
    connection: str
    kind: str
    tables: tuple[Table, ...]
    snapshot_version: int


class CellSchemas(Protocol):
    async def schema(self, connection: str, environment_id: str) -> Schema:
        """``connection`` as ``environment_id`` sees it; ``SchemaRefusal`` with the gateway's code,
        ``CellSchemaError`` when it could not be asked."""
        ...


class AgentCellSchemas(CellSchemas):
    """``CellSchemas`` through one cell's agent, with an ID token for its URL on every call."""

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

    async def schema(self, connection: str, environment_id: str) -> Schema:
        token = await self._id_tokens(self._url)
        try:
            response = await self._client.post(
                f"{self._url}/v1/datagw/schema",
                json={"connection": connection, "environment_id": environment_id},
                headers={"Authorization": f"Bearer {token}", ORG_HEADER: self._org},
                timeout=CALL_TIMEOUT_SECONDS,
            )
        except httpx2.HTTPError as exc:
            raise CellSchemaError(f"cell agent datagw schema: {type(exc).__name__}") from None
        try:
            parsed: object = response.json()
        except ValueError:
            parsed = None
        body = cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else {}
        if not response.is_success:
            raise CellSchemaError(
                f"cell agent datagw schema: HTTP {response.status_code} {body.get('code', '')}"
            )
        return schema_of(body)

    async def aclose(self) -> None:
        await self._client.aclose()


def schema_of(answer: Mapping[str, Any]) -> Schema:
    """The agent's ``{"status", "body"}``: a ``Schema`` for a 200, ``SchemaRefusal`` with the
    gateway's code (``error.code``, else ``code``) for anything else, ``CellSchemaError`` for an
    answer of neither shape."""
    status, body = answer.get("status"), answer.get("body")
    if not isinstance(status, int) or isinstance(status, bool):
        raise CellSchemaError("cell agent datagw schema: no status")
    doc = cast("dict[str, Any]", body) if isinstance(body, dict) else {}
    if status != 200:  # noqa: PLR2004
        raise SchemaRefusal(status, _code(doc))
    try:
        return Schema(
            connection=_text(doc["connection"]),
            kind=_text(doc["kind"]),
            tables=tuple(_table(t) for t in _list(doc["tables"])),
            snapshot_version=_int(doc["snapshot_version"]),
        )
    except (KeyError, TypeError) as exc:
        raise CellSchemaError(f"cell agent datagw schema: not a schema ({exc})") from None


def _code(doc: Mapping[str, Any]) -> str:
    error = doc.get("error")
    nested = cast("dict[str, Any]", error).get("code") if isinstance(error, dict) else None
    code = nested if isinstance(nested, str) else doc.get("code")
    return code if isinstance(code, str) else ""


def _table(raw: object) -> Table:
    doc = _dict(raw)
    return Table(
        name=_text(doc["name"]),
        columns=tuple(_column(c) for c in _list(doc["columns"])),
    )


def _column(raw: object) -> Column:
    doc = _dict(raw)
    db_type = doc.get("db_type")
    return Column(
        name=_text(doc["name"]),
        type=_text(doc["type"]),
        db_type=db_type if isinstance(db_type, str) else None,
    )


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("an object was expected")
    return cast("dict[str, Any]", value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("a list was expected")
    return cast("list[object]", value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("a string was expected")
    return value


def _int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("an integer was expected")
    return value


@dataclass(slots=True)
class FakeCellSchemas(CellSchemas):
    """In memory, for tests and local development: ``answers`` by connection name, a
    ``SchemaRefusal`` or ``CellSchemaError`` raised as it is; every call is recorded."""

    answers: dict[str, Schema | Exception] = field(default_factory=dict[str, "Schema | Exception"])
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    async def schema(self, connection: str, environment_id: str) -> Schema:
        self.calls.append((connection, environment_id))
        found = self.answers.get(connection)
        if found is None:
            raise SchemaRefusal(403, "CONNECTION_NOT_GRANTED")
        if isinstance(found, Exception):
            raise found
        return found


type CacheKey = tuple[str, str, str]


class SchemaCache:
    """Answers kept for ``seconds`` by (org, environment, connection), at most ``entries`` of
    them (the oldest goes first). ``clock`` is the seam tests move."""

    def __init__(
        self,
        *,
        seconds: float = CACHE_SECONDS,
        entries: int = CACHE_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._seconds = seconds
        self._entries = entries
        self._clock = clock
        self._kept: OrderedDict[CacheKey, tuple[Schema, float]] = OrderedDict()

    def get(self, key: CacheKey) -> Schema | None:
        kept = self._kept.get(key)
        if kept is None:
            return None
        if self._clock() >= kept[1]:
            del self._kept[key]
            return None
        return kept[0]

    def put(self, key: CacheKey, schema: Schema) -> None:
        self._kept.pop(key, None)
        self._kept[key] = (schema, self._clock() + self._seconds)
        while len(self._kept) > self._entries:
            self._kept.popitem(last=False)


__all__ = [
    "CACHE_SECONDS",
    "AgentCellSchemas",
    "CellSchemaError",
    "CellSchemas",
    "Column",
    "FakeCellSchemas",
    "Schema",
    "SchemaCache",
    "SchemaRefusal",
    "Table",
    "schema_of",
]
