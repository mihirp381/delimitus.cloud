"""Connection kinds (GA-5): the ten read-only sources a connection can point at, and the
non-secret address of each.

A connection has a ``kind``; the data gateway picks its connector by it
(``ssc_datagw.kinds``). The address is what an admin types when the connection is created: where
the source is, never how to log in. Credentials go through the cell's secret intake and never
through the control plane (SSC-051).

``AVAILABLE`` lists the kinds a connector exists for. A kind that is not available cannot be
created (``CONNECTOR_UNAVAILABLE``), so the console never shows a source nobody has run. A
connector lands with its kind added here and its own module; nothing else in the control plane
changes.
"""

from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

Kind = Literal[
    "postgres",
    "mysql",
    "sqlserver",
    "bigquery",
    "snowflake",
    "gsheets",
    "gcs",
    "s3",
    "airtable",
    "rest",
]
KINDS: Final[tuple[Kind, ...]] = (
    "postgres",
    "mysql",
    "sqlserver",
    "bigquery",
    "snowflake",
    "gsheets",
    "gcs",
    "s3",
    "airtable",
    "rest",
)
SQL_KINDS: Final[frozenset[Kind]] = frozenset({"postgres", "mysql", "sqlserver"})
"""Kinds with a host, a port and a database, whose statements the classifier reads as SQL."""
DEFAULT_PORT: Final[dict[Kind, int]] = {"postgres": 5432, "mysql": 3306, "sqlserver": 1433}
AVAILABLE: Final[frozenset[Kind]] = frozenset({"postgres", "mysql", "rest", "gsheets", "s3"})
"""The kinds a connector exists for. Grows one kind per connector commit."""

TITLE: Final[dict[Kind, str]] = {
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "sqlserver": "SQL Server",
    "bigquery": "BigQuery",
    "snowflake": "Snowflake",
    "gsheets": "Google Sheets",
    "gcs": "Google Cloud Storage",
    "s3": "Amazon S3",
    "airtable": "Airtable",
    "rest": "REST (GET)",
}

Host = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9.-]{1,255}$")]
Port = Annotated[int, Field(ge=1, le=65535)]
DatabaseName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,63}$")]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
Prefix = Annotated[str, StringConstraints(pattern=r"^[^\x00-\x1f]{0,512}$")]


class _Address(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SqlAddress(_Address):
    """PostgreSQL, MySQL and SQL Server: a host, a port and a database."""

    host: Host
    port: Port
    database: DatabaseName


class BigQueryAddress(_Address):
    project: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")]
    dataset: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_]{1,1024}$")]
    location: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,32}$")] = "US"


class SnowflakeAddress(_Address):
    """``account`` is the account identifier (``<org>-<account>``), never a URL."""

    account: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
    database: Identifier
    schema_name: Identifier = Field(default="PUBLIC", alias="schema")
    warehouse: Identifier
    role: Identifier | None = None

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class SheetsAddress(_Address):
    """One spreadsheet by id; ``sheet`` is the tab apps read, every tab when left out."""

    spreadsheet_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{20,128}$")]
    sheet: Annotated[str, StringConstraints(pattern=r"^[^\x00-\x1f'!:]{1,100}$")] | None = None


class GcsAddress(_Address):
    bucket: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{1,221}[a-z0-9]$")]
    prefix: Prefix = ""


class S3Address(_Address):
    bucket: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")]
    prefix: Prefix = ""
    region: Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")]


class AirtableAddress(_Address):
    base_id: Annotated[str, StringConstraints(pattern=r"^app[A-Za-z0-9]{14}$")]
    table: Annotated[str, StringConstraints(pattern=r"^[^\x00-\x1f]{1,100}$")] | None = None


class RestAddress(_Address):
    """``base_url`` is https and fixes the host; a request path is appended to it."""

    base_url: Annotated[
        str, StringConstraints(pattern=r"^https://[A-Za-z0-9.-]{1,255}(/[^\s?#]*)?$")
    ]


Address = (
    SqlAddress
    | BigQueryAddress
    | SnowflakeAddress
    | SheetsAddress
    | GcsAddress
    | S3Address
    | AirtableAddress
    | RestAddress
)

ADDRESS_OF: Final[dict[Kind, type[Address]]] = {
    "postgres": SqlAddress,
    "mysql": SqlAddress,
    "sqlserver": SqlAddress,
    "bigquery": BigQueryAddress,
    "snowflake": SnowflakeAddress,
    "gsheets": SheetsAddress,
    "gcs": GcsAddress,
    "s3": S3Address,
    "airtable": AirtableAddress,
    "rest": RestAddress,
}


class AddressError(ValueError):
    """The address does not fit the kind. The message names fields, never values."""


def parse_address(kind: Kind, data: dict[str, Any]) -> Address:
    """``data`` as the kind's address. A SQL kind's port defaults to the engine's."""
    if kind in SQL_KINDS and "port" not in data:
        data = {**data, "port": DEFAULT_PORT[kind]}
    try:
        return ADDRESS_OF[kind].model_validate(data)
    except ValidationError as exc:
        fields = sorted({".".join(map(str, e["loc"])) or "(the address)" for e in exc.errors()})
        raise AddressError(f"address for {kind}: {', '.join(fields)}") from None


def address_json(address: Address) -> dict[str, Any]:
    """The address as stored: by alias (``schema``), defaults included, so a reader needs no
    knowledge of which members were typed."""
    return address.model_dump(mode="json", by_alias=True)
