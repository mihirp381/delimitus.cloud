"""The connector registry (GA-5): a connection's kind picks its target model and connector."""

import json

import pytest
from pki import make_pki

from ssc_contracts import connections as contract
from ssc_datagw.bigquery import BigQueryConnector, BigQueryTarget
from ssc_datagw.gcs import GcsConnector, GcsTarget
from ssc_datagw.gsheets import GsheetsConnector, GsheetsTarget
from ssc_datagw.kinds import (
    AVAILABLE,
    REGISTRY,
    Registered,
    TargetError,
    connector_for,
    parse_target,
)
from ssc_datagw.mysql import MySqlConnector, MySqlTarget
from ssc_datagw.postgres import PostgresConnector, PostgresTarget
from ssc_datagw.rest import RestConnector, RestTarget
from ssc_datagw.s3 import S3Connector, S3Target
from ssc_datagw.sqlserver import SqlServerConnector, SqlServerTarget

PASSWORD = "fake-" + "registry-" + "password"
TARGET = {"host": "10.0.0.5", "database": "sales", "user": "ssc_datagw", "password": PASSWORD}


def test_the_gateway_serves_exactly_the_kinds_the_control_plane_lets_a_customer_create() -> None:
    assert AVAILABLE == contract.AVAILABLE
    assert AVAILABLE <= set(contract.KINDS)


def test_a_connection_without_a_kind_is_postgres_as_before_ga_5() -> None:
    target = parse_target(json.dumps(TARGET))
    assert target == PostgresTarget.model_validate({**TARGET, "kind": "postgres"})
    assert target == parse_target(json.dumps({**TARGET, "kind": "postgres"}))
    assert isinstance(connector_for(target), PostgresConnector)


def test_a_mysql_connection_parses_to_a_mysql_target_and_its_connector() -> None:
    target = parse_target(json.dumps({**TARGET, "kind": "mysql"}))
    assert target == MySqlTarget.model_validate({**TARGET, "kind": "mysql"})
    assert isinstance(target, MySqlTarget)
    assert (target.port, target.database) == (3306, "sales")
    assert isinstance(connector_for(target), MySqlConnector)


def test_a_rest_connection_parses_to_a_rest_target_and_its_connector() -> None:
    raw = {"kind": "rest", "base_url": "https://api.example.com/v2", "token": PASSWORD}
    target = parse_target(json.dumps(raw))
    assert target == RestTarget.model_validate(raw)
    assert isinstance(target, RestTarget)
    assert target.token is not None and target.token.get_secret_value() == PASSWORD
    assert isinstance(connector_for(target), RestConnector)


def test_a_gsheets_connection_names_the_key_field_and_never_its_text() -> None:
    raw = {
        "kind": "gsheets",
        "spreadsheet_id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
        "service_account": '{"client_email": "x", "private_key": "' + PASSWORD + '"}',
    }
    with pytest.raises(TargetError) as caught:
        parse_target(json.dumps(raw))
    assert caught.value.fields == ("service_account",)
    assert PASSWORD not in str(caught.value)
    assert REGISTRY["gsheets"] == Registered(GsheetsTarget, GsheetsConnector)


def test_an_s3_connection_parses_to_an_s3_target_and_its_connector() -> None:
    raw = {
        "kind": "s3",
        "bucket": "corp-exports",
        "region": "eu-west-1",
        "prefix": "exports/",
        "access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "secret_access_key": PASSWORD,
    }
    target = parse_target(json.dumps(raw))
    assert target == S3Target.model_validate(raw)
    assert isinstance(target, S3Target)
    assert isinstance(connector_for(target), S3Connector)
    assert PASSWORD not in repr(target)


def test_a_bigquery_connection_names_the_key_field_and_never_its_text() -> None:
    raw = {
        "kind": "bigquery",
        "project": "corp-analytics",
        "dataset": "warehouse",
        "service_account": '{"client_email": "x", "private_key": "' + PASSWORD + '"}',
    }
    with pytest.raises(TargetError) as caught:
        parse_target(json.dumps(raw))
    assert caught.value.fields == ("service_account",)
    assert PASSWORD not in str(caught.value)
    assert REGISTRY["bigquery"] == Registered(BigQueryTarget, BigQueryConnector)


def test_a_sqlserver_connection_parses_to_its_target_and_connector() -> None:
    raw = {**TARGET, "kind": "sqlserver", "ca": make_pki().ca}
    target = parse_target(json.dumps(raw))
    assert target == SqlServerTarget.model_validate(raw)
    assert isinstance(target, SqlServerTarget)
    assert target.port == 1433
    assert isinstance(connector_for(target), SqlServerConnector)
    assert PASSWORD not in repr(target)


def test_a_gcs_connection_names_the_key_field_and_never_its_text() -> None:
    raw = {
        "kind": "gcs",
        "bucket": "corp-exports",
        "prefix": "exports/",
        "service_account": '{"client_email": "x", "private_key": "' + PASSWORD + '"}',
    }
    with pytest.raises(TargetError) as caught:
        parse_target(json.dumps(raw))
    assert caught.value.fields == ("service_account",)
    assert PASSWORD not in str(caught.value)
    assert REGISTRY["gcs"] == Registered(GcsTarget, GcsConnector)


def test_a_rest_connection_with_a_database_address_names_the_field() -> None:
    with pytest.raises(TargetError) as caught:
        parse_target(json.dumps({**TARGET, "kind": "rest"}))
    assert "base_url" in caught.value.fields
    assert PASSWORD not in str(caught.value)


@pytest.mark.parametrize(
    ("raw", "field"),
    [
        ("{" + PASSWORD, "(the JSON)"),
        (json.dumps({**TARGET, "kind": "oracle"}), "kind"),
        (json.dumps({**TARGET, "kind": "airtable"}), "kind"),  # a kind with no connector yet
        (json.dumps({k: v for k, v in TARGET.items() if k != "host"}), "host"),
        (json.dumps({**TARGET, "sslmode": "disable"}), "sslmode"),
    ],
)
def test_a_wrong_connection_names_the_field_and_never_the_credential(raw: str, field: str) -> None:
    with pytest.raises(TargetError) as caught:
        parse_target(raw)
    assert field in str(caught.value)
    assert PASSWORD not in str(caught.value)
    assert PASSWORD not in repr(caught.value.fields)


def test_every_registered_target_names_its_kind_and_hides_its_credential() -> None:
    for kind, entry in REGISTRY.items():
        assert entry.target.model_fields["kind"].default == kind
    target = parse_target(json.dumps(TARGET))
    assert PASSWORD not in repr(target)
    assert PASSWORD not in str(target)
