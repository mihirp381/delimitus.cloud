"""Which connector serves each kind of connection (GA-5).

A connection's ``SSC_CONNECTION_*`` JSON names its ``kind`` (``ssc_contracts.connections.KINDS``;
left out, it is ``postgres``, the only kind before GA-5, so every pinned secret from before still
reads). The kind picks the target model that validates the rest of the JSON and the connector
that serves it. :data:`REGISTRY` is the one place a kind is named in the gateway: a connector
commit adds its entry here and its kind to ``ssc_contracts.connections.AVAILABLE``, and a test
keeps the two sets equal, so the control plane never lets a customer create a connection the
gateway of the same commit cannot serve.

An error names the variable's fields, never a value, which holds a credential.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from ssc_contracts.connections import Kind
from ssc_datagw.connectors import Connector
from ssc_datagw.mysql import MySqlConnector, MySqlTarget
from ssc_datagw.postgres import PostgresConnector, PostgresTarget

type Target = PostgresTarget | MySqlTarget
"""Every target model a kind may validate to; grows with each connector."""


class TargetError(ValueError):
    """A connection JSON that no kind accepts. ``fields`` names what was wrong, never a value."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        super().__init__(", ".join(fields))
        self.fields = fields


@dataclass(frozen=True, slots=True)
class Registered:
    """``target`` validates the connection JSON; ``connect`` builds the connector from it."""

    target: type[Target]
    connect: Callable[[Any], Connector]


REGISTRY: Final[Mapping[Kind, Registered]] = {
    "postgres": Registered(PostgresTarget, PostgresConnector),
    "mysql": Registered(MySqlTarget, MySqlConnector),
}

AVAILABLE: Final[frozenset[Kind]] = frozenset(REGISTRY)
"""The kinds this build of the gateway serves."""


class _Kinded(BaseModel):
    """The one member read before the kind's own model: everything else is ignored here."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: Kind = "postgres"


def _fields(exc: ValidationError) -> tuple[str, ...]:
    return tuple(sorted({".".join(map(str, e["loc"])) or "(the JSON)" for e in exc.errors()}))


def parse_target(raw: str) -> Target:
    """The target a connection's JSON describes, validated by its kind's model."""
    try:
        kind = _Kinded.model_validate_json(raw).kind
    except ValidationError as exc:
        raise TargetError(_fields(exc)) from None
    entry = REGISTRY.get(kind)
    if entry is None:
        raise TargetError((f"kind ({kind} has no connector in this build)",))
    try:
        return entry.target.model_validate_json(raw)
    except ValidationError as exc:
        raise TargetError(_fields(exc)) from None


def connector_for(target: Target) -> Connector:
    """The connector that serves ``target``, by its kind."""
    return REGISTRY[target.kind].connect(target)
