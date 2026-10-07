"""Connection kinds (GA-5): the ten kinds and the address each one takes."""

import pytest

from ssc_contracts.connections import (
    ADDRESS_OF,
    AVAILABLE,
    DEFAULT_PORT,
    KINDS,
    SQL_KINDS,
    TITLE,
    AddressError,
    SnowflakeAddress,
    SqlAddress,
    address_json,
    parse_address,
)

GOOD = {
    "postgres": {"host": "db.corp.internal", "database": "warehouse"},
    "mysql": {"host": "mysql.corp.internal", "port": 3307, "database": "shop"},
    "sqlserver": {"host": "sql.corp.internal", "database": "Finance"},
    "bigquery": {"project": "acme-analytics", "dataset": "sales"},
    "snowflake": {"account": "acme-xy12345", "database": "SALES", "warehouse": "WH_SMALL"},
    "gsheets": {"spreadsheet_id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"},
    "gcs": {"bucket": "acme-exports", "prefix": "finance/2026/"},
    "s3": {"bucket": "acme-exports", "region": "eu-west-1"},
    "airtable": {"base_id": "appAbCdEfGhIjKlMn"},
    "rest": {"base_url": "https://api.corp.example/v2"},
}


def test_every_kind_has_a_title_an_address_model_and_an_example() -> None:
    assert set(KINDS) == set(TITLE) == set(ADDRESS_OF) == set(GOOD)
    assert AVAILABLE <= set(KINDS)
    assert set(DEFAULT_PORT) == SQL_KINDS


@pytest.mark.parametrize("kind", KINDS)
def test_each_example_parses_and_dumps_with_its_defaults(kind: str) -> None:
    parsed = parse_address(kind, GOOD[kind])  # pyright: ignore[reportArgumentType]
    dumped = address_json(parsed)
    for key, value in GOOD[kind].items():
        assert dumped[key] == value
    if kind in SQL_KINDS:
        assert isinstance(parsed, SqlAddress)
        assert dumped["port"] == GOOD[kind].get("port", DEFAULT_PORT[kind])  # pyright: ignore[reportArgumentType]
    else:
        assert not isinstance(parsed, SqlAddress)


def test_snowflake_s_schema_is_spelled_schema_on_the_wire() -> None:
    parsed = parse_address("snowflake", {**GOOD["snowflake"], "schema": "FINANCE"})
    assert isinstance(parsed, SnowflakeAddress)
    assert parsed.schema_name == "FINANCE"
    assert address_json(parsed)["schema"] == "FINANCE"
    assert address_json(parse_address("snowflake", GOOD["snowflake"]))["schema"] == "PUBLIC"


BAD = [
    ("postgres", {"database": "warehouse"}, "host"),
    ("postgres", {"host": "db corp", "database": "warehouse"}, "host"),
    ("mysql", {"host": "mysql.corp.internal", "port": 0, "database": "shop"}, "port"),
    ("bigquery", {"project": "Acme", "dataset": "sales"}, "project"),
    ("bigquery", {"project": "acme-analytics"}, "dataset"),
    (
        "snowflake",
        {"account": "https://x.snowflakecomputing.com", "database": "D", "warehouse": "W"},
        "account",
    ),
    ("gsheets", {"spreadsheet_id": "short"}, "spreadsheet_id"),
    ("gcs", {"bucket": "Bad_Bucket"}, "bucket"),
    ("s3", {"bucket": "acme-exports", "region": "mars"}, "region"),
    ("airtable", {"base_id": "tblAbCdEfGhIjKlMn"}, "base_id"),
    ("rest", {"base_url": "http://api.corp.example"}, "base_url"),
    ("rest", {"base_url": "https://api.corp.example", "token": "x"}, "token"),
    (
        "postgres",
        {"host": "db.corp.internal", "database": "shop", "password": "hunter2"},
        "password",
    ),
]


@pytest.mark.parametrize(("kind", "data", "field"), BAD)
def test_a_wrong_address_names_the_field_and_never_the_value(
    kind: str, data: dict[str, object], field: str
) -> None:
    with pytest.raises(AddressError) as caught:
        parse_address(kind, data)  # pyright: ignore[reportArgumentType]
    assert field in str(caught.value)
    for value in data.values():
        assert str(value) not in str(caught.value) or str(value) == field
