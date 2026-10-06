"""SSC-018: every app gets the cell's identity keys and its own origin, as plain values.

* a note the gateway's signer makes verifies, offline, with the env ``desired_for`` gives
              -> test_a_note_the_gateway_signs_verifies_with_the_env_desired_for_gives
* an absent setting leaves its value out          -> test_an_absent_setting_leaves_its_value_out
* a deployment carries the env                    -> test_a_deployment_runs_with_the_identity_env
* an app already running gets it on the next reconcile, as drift
              -> test_a_running_app_gets_the_identity_env_from_the_reconciler
"""

from __future__ import annotations

import base64
import json
import socket
from dataclasses import replace
from typing import Any

import pytest
import test_deploy
from cryptography.hazmat.primitives.asymmetric import ec
from test_deploy import Bench, build_release, deploy, manifest_of, run, start_deploy

from ssc_app.identity import IdentityRefused, IdentityVerifier
from ssc_contracts import app_env
from ssc_contracts.ids import new_id
from ssc_control.runtime.driver import (
    AppIdentity,
    EnvironmentRow,
    ReleaseRow,
    ServiceSpec,
    desired_for,
    service_name,
)
from ssc_control.runtime.reconciler import reconcile_env
from ssc_control.worker import CompositionError, app_identity_from_env
from ssc_edge.identity_note import compose_note, jwks, sign_note

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b

LABEL = "cellabcd01"
ISSUER = f"https://keys.delimitus.com/{LABEL}"
HOSTS = {
    "prod": f"ledger.{LABEL}.delimitusapps.com",
    "preview": f"ledger--preview.{LABEL}.delimitusapps.com",
}
NOW = 1_790_000_000
OLD, NEW = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
PUBLISHED = json.dumps(jwks((OLD.public_key(), "k-old"), (NEW.public_key(), "k-new")), indent=2)
SETTINGS = {"SSC_IDENTITY_JWKS": PUBLISHED, "SSC_IDENTITY_ISSUER": ISSUER}
IDENTITY_NAMES = {app_env.IDENTITY_KEYS_URL, app_env.APP_ORIGIN}


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any network use fails the test."""

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("the network was used")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def spec_of(env: str, identity: AppIdentity | None, slug: str = "ledger") -> ServiceSpec:
    desired = desired_for(
        env=EnvironmentRow(id=new_id("env"), org_id=new_id("org"), app_id=new_id("app"), name=env),
        release=ReleaseRow(id=new_id("rel"), image_digest="sha256:" + "a" * 64),
        manifest=manifest_of(),
        app_status="active",
        slug=slug,
        identity=identity,
    )
    assert isinstance(desired, ServiceSpec)
    return desired


def note_for(host: str, env: str, key: ec.EllipticCurvePrivateKey, kid: str) -> str:
    note = compose_note(
        issuer=ISSUER,
        audience=f"https://{host}",
        subject="usr_11111111111111111111",
        org="org_0123456789abcdefghij",
        app="app_aaaaaaaaaaaaaaaaaaaa",
        env=env,
        role="user",
        now=NOW,
    )
    return sign_note(note, private_key=key, kid=kid)


def serving_env(b: Bench, env: str) -> dict[str, str]:
    svc = b.runtime.services[service_name(env)]
    (serving,) = [r for r in svc.revisions if svc.traffic.get(r.name) == 100]
    return dict(serving.env)


# ── the env ──────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("offline")
@pytest.mark.parametrize("env", ["prod", "preview"])
def test_a_note_the_gateway_signs_verifies_with_the_env_desired_for_gives(env: str) -> None:
    spec = spec_of(env, app_identity_from_env(SETTINGS))
    plain = dict(spec.env)
    assert plain[app_env.APP_ORIGIN] == f"https://{HOSTS[env]}"
    assert plain[app_env.STREAMLIT_ALLOWED_ORIGINS] == plain[app_env.APP_ORIGIN]
    assert plain[app_env.IDENTITY_KEYS_URL].startswith("data:application/json;base64,")
    assert not IDENTITY_NAMES & set(spec.secrets)
    verifier = IdentityVerifier(
        audience=plain[app_env.APP_ORIGIN], keys=plain[app_env.IDENTITY_KEYS_URL], issuer=ISSUER
    )
    for key, kid in ((NEW, "k-new"), (OLD, "k-old")):
        note = verifier.verify(note_for(HOSTS[env], env, key, kid), now=NOW + 1)
        assert (note.aud, note.env) == (f"https://{HOSTS[env]}", env)
    other = "preview" if env == "prod" else "prod"
    refusals = {
        "wrong_audience": note_for(HOSTS[other], other, NEW, "k-new"),
        "unknown_key": note_for(HOSTS[env], env, ec.generate_private_key(ec.SECP256R1()), "k-x"),
    }
    for code, token in refusals.items():
        with pytest.raises(IdentityRefused) as refused:
            verifier.verify(token, now=NOW + 1)
        assert refused.value.code == code


@pytest.mark.parametrize(
    ("settings", "names"),
    [
        ({}, set[str]()),
        ({"SSC_IDENTITY_JWKS": PUBLISHED}, {app_env.IDENTITY_KEYS_URL}),
        ({"SSC_IDENTITY_ISSUER": ISSUER}, {app_env.APP_ORIGIN}),
        (SETTINGS, IDENTITY_NAMES),
    ],
)
def test_an_absent_setting_leaves_its_value_out(settings: dict[str, str], names: set[str]) -> None:
    identity = app_identity_from_env(settings)
    assert (identity is None) == (not settings)
    plain = dict(spec_of("prod", identity).env)
    assert IDENTITY_NAMES & set(plain) == names
    assert (app_env.STREAMLIT_ALLOWED_ORIGINS in plain) == (app_env.APP_ORIGIN in names)
    assert {app_env.PORT, app_env.HOME} <= set(plain)


def test_the_jwks_is_inlined_compactly_so_its_whitespace_never_changes_a_spec() -> None:
    compact = json.dumps(json.loads(PUBLISHED))
    spaced = app_identity_from_env(SETTINGS)
    tight = app_identity_from_env({**SETTINGS, "SSC_IDENTITY_JWKS": compact})
    assert spaced == tight
    assert spaced is not None and spaced.keys_url is not None
    payload = base64.b64decode(spaced.keys_url.removeprefix("data:application/json;base64,"))
    assert json.loads(payload) == json.loads(PUBLISHED)
    assert spec_of("prod", spaced).spec_fingerprint == spec_of("prod", tight).spec_fingerprint


def test_a_slug_from_before_the_host_rule_gets_the_keys_but_no_origin() -> None:
    plain = dict(spec_of("prod", app_identity_from_env(SETTINGS), slug="Old_Slug").env)
    assert IDENTITY_NAMES & set(plain) == {app_env.IDENTITY_KEYS_URL}


@pytest.mark.parametrize(
    "settings",
    [
        {"SSC_IDENTITY_JWKS": "not json"},
        {"SSC_IDENTITY_JWKS": '{"kty": "EC"}'},
        {"SSC_IDENTITY_ISSUER": "https://keys.example.com/cellabcd01"},
        {"SSC_IDENTITY_ISSUER": "https://keys.delimitus.com/Cell-01"},
        {**SETTINGS, "SSC_APPS_DOMAIN": "localhost"},
    ],
)
def test_a_bad_identity_setting_stops_the_worker_starting(settings: dict[str, str]) -> None:
    with pytest.raises(CompositionError):
        app_identity_from_env(settings)


# ── deployments and the reconciler ───────────────────────────────────────────


async def test_a_deployment_runs_with_the_identity_env(b: Bench) -> None:
    identity = app_identity_from_env(SETTINGS)
    assert identity is not None
    ports = replace(b.ports, app_identity=identity)
    release = await build_release(b, b.w.preview)
    r = start_deploy(b, b.w.preview, release)
    assert r.status_code == 202, r.text
    assert await run(b, str(r.json()["operation_id"]), ports) == "healthy"
    plain = serving_env(b, b.w.preview)
    assert plain[app_env.APP_ORIGIN] == f"https://ledger--preview.{LABEL}.delimitusapps.com"
    assert plain[app_env.IDENTITY_KEYS_URL] == identity.keys_url


async def test_a_running_app_gets_the_identity_env_from_the_reconciler(b: Bench) -> None:
    release = await build_release(b, b.w.preview)
    _, state = await deploy(b, b.w.preview, release)
    assert state == "healthy"
    assert not IDENTITY_NAMES & set(serving_env(b, b.w.preview))

    async def reconcile(identity: AppIdentity | None) -> Any:
        return await reconcile_env(
            b.ports.engine,
            b.runtime,
            b.ports.release_specs,
            org_id=b.w.org,
            env_id=b.w.preview,
            identity=identity,
        )

    assert (await reconcile(None)).kind == "converged"
    identity = app_identity_from_env(SETTINGS)
    passes = [await reconcile(identity) for _ in range(3)]
    assert [(p.kind, p.change and p.change.kind) for p in passes] == [
        ("changed", "apply"),
        ("changed", "set_traffic"),
        ("converged", None),
    ]
    assert IDENTITY_NAMES <= set(serving_env(b, b.w.preview))
