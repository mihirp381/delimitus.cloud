"""The connector registry (GA-5): a connection's kind picks its target model and connector."""

import json

import pytest

from ssc_contracts import connections as contract
from ssc_datagw.kinds import AVAILABLE, REGISTRY, TargetError, connector_for, parse_target
from ssc_datagw.postgres import PostgresConnector, PostgresTarget

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


@pytest.mark.parametrize(
    ("raw", "field"),
    [
        ("{" + PASSWORD, "(the JSON)"),
        (json.dumps({**TARGET, "kind": "oracle"}), "kind"),
        (json.dumps({**TARGET, "kind": "mysql"}), "kind"),  # a kind with no connector yet
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
