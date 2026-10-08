"""The data gateway's environment (SSC-050): what infra sets, read strictly."""

import json

import pytest
from datagw_world import ENV, NOTE_JWKS, SALES, SETTINGS

from ssc_datagw.postgres import PostgresTarget
from ssc_datagw.settings import MAX_STALE_SECONDS, SettingsError, settings_from_env

PASSWORD = "fake-" + "connection-" + "password"
TARGET = {"host": "10.0.0.5", "database": "sales", "user": "ssc_datagw", "password": PASSWORD}
CONNECTION_VAR = "SSC_CONNECTION_" + SALES.upper()


def test_the_cell_environment_reads_as_the_settings() -> None:
    assert settings_from_env(ENV) == SETTINGS
    assert settings_from_env({**ENV, "SSC_DATAGW_AUDIENCE": SETTINGS.audience + "/"}) == SETTINGS


def test_the_defaults() -> None:
    env = {k: v for k, v in ENV.items() if k != "SSC_APPS_DOMAIN"}
    got = settings_from_env(env)
    assert got.apps_domain == "delimitusapps.com"
    assert got.issuer == f"https://keys.delimitus.com/{SETTINGS.cell_label}"
    assert got.max_stale == MAX_STALE_SECONDS
    assert settings_from_env({**ENV, "SSC_SNAPSHOT_MAX_AGE": "30"}).max_stale == 30


def test_file_links_are_signed_as_the_gateway_s_own_account() -> None:
    assert SETTINGS.signer == f"ssc-data@{SETTINGS.project_id}.iam.gserviceaccount.com"


PRIVATE = {"keys": [{**NOTE_JWKS["keys"][0], "d": "secret-part"}]}
BAD = {
    "no org": {"SSC_ORG_ID": ""},
    "a bad org": {"SSC_ORG_ID": "org_UPPER"},
    "a bad project": {"SSC_PROJECT_ID": "Not_A_Project"},
    "an http audience": {"SSC_DATAGW_AUDIENCE": "http://datagw.run.app"},
    "a bad cell label": {"SSC_CELL_LABEL": "aeiou"},
    "a bad apps domain": {"SSC_APPS_DOMAIN": "not a domain"},
    "no bucket": {"SSC_CELL_BUCKET": ""},
    "a jwks not json": {"SSC_IDENTITY_JWKS": "{"},
    "a jwks without keys": {"SSC_IDENTITY_JWKS": json.dumps({"keys": []})},
    "a private jwks": {"SSC_IDENTITY_JWKS": json.dumps(PRIVATE)},
    "a max age over 120": {"SSC_SNAPSHOT_MAX_AGE": "121"},
    "a max age of 0": {"SSC_SNAPSHOT_MAX_AGE": "0"},
    "a max age not a number": {"SSC_SNAPSHOT_MAX_AGE": "soon"},
    "a connection not json": {CONNECTION_VAR: "{" + PASSWORD},
    "a connection without a host": {
        CONNECTION_VAR: json.dumps({k: v for k, v in TARGET.items() if k != "host"})
    },
    "a connection with an unknown field": {
        CONNECTION_VAR: json.dumps({**TARGET, "sslmode": "disable"})
    },
    "a connection with a bad port": {CONNECTION_VAR: json.dumps({**TARGET, "port": 0})},
    "a connection of a kind this build lacks": {
        CONNECTION_VAR: json.dumps({**TARGET, "kind": "snowflake"})
    },
    "a connection of no kind at all": {CONNECTION_VAR: json.dumps({**TARGET, "kind": "oracle"})},
    "a connection with a bad id": {"SSC_CONNECTION_SALES": json.dumps(TARGET)},
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_a_bad_environment_is_refused(case: str) -> None:
    with pytest.raises(SettingsError) as e:
        settings_from_env({**ENV, **BAD[case]})
    assert "secret-part" not in str(e.value)
    assert PASSWORD not in str(e.value)


def test_each_connection_variable_is_a_target_and_its_password_is_never_shown() -> None:
    got = settings_from_env({**ENV, CONNECTION_VAR: json.dumps({**TARGET, "port": 6432})})
    assert got.connections == {SALES: PostgresTarget.model_validate({**TARGET, "port": 6432})}
    sales = got.connections[SALES]
    assert isinstance(sales, PostgresTarget)
    assert sales.password.get_secret_value() == PASSWORD
    assert PASSWORD not in repr(got)
    assert settings_from_env(ENV).connections == {}
