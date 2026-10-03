"""The data gateway's environment (SSC-050): what infra sets, read strictly."""

import json

import pytest
from datagw_world import ENV, NOTE_JWKS, SETTINGS

from ssc_datagw.settings import MAX_STALE_SECONDS, SettingsError, settings_from_env


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
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_a_bad_environment_is_refused(case: str) -> None:
    with pytest.raises(SettingsError) as e:
        settings_from_env({**ENV, **BAD[case]})
    assert "secret-part" not in str(e.value)
