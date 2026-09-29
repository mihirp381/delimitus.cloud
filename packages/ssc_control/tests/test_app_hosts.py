"""SSC-042's host rule in the API (decision 004): a slug the rule refuses is 422 before the
database, and each environment's url follows the rule once the org has a cell label."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from ssc_testkit import ISSUER, Dsns, SigningKey, assert_problem, auth, make_org, mint, new_key

from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api import Settings, create_app
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.api.problems import REQUEST_ID_HEADER
from ssc_control.db import CreatedOrg, bind_org_sync

DOMAIN = "apps.test"
LABEL = "k7q2m9xa"


@dataclass(frozen=True)
class Hosts:
    dsns: Dsns
    client: TestClient
    org: CreatedOrg
    token: str


@pytest.fixture(scope="module")
def h(dsns: Dsns, signing_key: SigningKey) -> Iterator[Hosts]:
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
        apps_domain=DOMAIN,
    )
    org = make_org(dsns.app, "Hosts")
    token = mint(signing_key, org=org.org_id, sub=org.admin_user_id)
    with TestClient(create_app(settings)) as client:
        yield Hosts(dsns, client, org, token)


def create(h: Hosts, slug: str) -> Response:
    return h.client.post(
        "/v1/apps", json={"slug": slug}, headers=auth(h.token, **{IDEMPOTENCY_HEADER: new_key()})
    )


def app_rows(h: Hosts, slug: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(h.dsns.app) as conn:
        bind_org_sync(conn, h.org.org_id)
        return conn.execute("select id from ssc.app where slug = %s", (slug,)).fetchall()


def logged_evidence(caplog: pytest.LogCaptureFixture, r: Response) -> dict[str, Any]:
    rid = r.headers[REQUEST_ID_HEADER]
    for record in caplog.records:
        line = record.getMessage()
        if line.startswith("refusal ") and json.loads(line[8:])["request_id"] == rid:
            evidence: dict[str, Any] = json.loads(line[8:])["evidence"]
            return evidence
    raise AssertionError(f"no refusal logged for {rid}")


def urls(app: dict[str, Any]) -> dict[str, str | None]:
    return {e["name"]: e["url"] for e in app["environments"]}


def set_cell_label(h: Hosts, label: str | None) -> None:
    with psycopg.connect(h.dsns.superuser) as conn:
        conn.execute("update ssc.org set cell_label = %s where id = %s", (label, h.org.org_id))


# ── slugs ────────────────────────────────────────────────────────────────────


def _refused_by_the_model(caplog: pytest.LogCaptureFixture, h: Hosts, slug: str) -> None:
    with caplog.at_level(logging.WARNING, logger="ssc.api"):
        r = create(h, slug)
    assert_problem(r, ErrorCode.VALIDATION_FAILED)
    errors = logged_evidence(caplog, r)["errors"]
    assert [(e["loc"], e["type"]) for e in errors] == [(["body", "slug"], "value_error")]
    assert slug not in r.text
    assert app_rows(h, slug) == []


def test_slug_double_dash_422_before_db(h: Hosts, caplog: pytest.LogCaptureFixture) -> None:
    _refused_by_the_model(caplog, h, "a--b")


@pytest.mark.parametrize("slug", ["api", "www", "console", "keys", "xn--bcher-kva"])
def test_reserved_and_punycode_slugs_422_before_db(
    h: Hosts, caplog: pytest.LogCaptureFixture, slug: str
) -> None:
    _refused_by_the_model(caplog, h, slug)


def test_a_slug_near_a_reserved_word_is_fine(h: Hosts) -> None:
    assert create(h, "apis").status_code == 201
    assert create(h, "my-console").status_code == 201


# ── urls ─────────────────────────────────────────────────────────────────────


def test_url_is_null_until_the_org_has_a_cell_label(h: Hosts) -> None:
    set_cell_label(h, None)
    r = create(h, "expenses")
    assert r.status_code == 201, r.text
    app = r.json()
    assert urls(app) == {"prod": None, "preview": None}
    try:
        set_cell_label(h, LABEL)
        got = h.client.get(f"/v1/apps/{app['id']}", headers=auth(h.token)).json()
        assert urls(got) == {
            "prod": f"https://expenses.{LABEL}.{DOMAIN}",
            "preview": f"https://expenses--preview.{LABEL}.{DOMAIN}",
        }
        created = create(h, "payroll").json()
        assert urls(created)["prod"] == f"https://payroll.{LABEL}.{DOMAIN}"
    finally:
        set_cell_label(h, None)


def test_a_slug_stored_before_the_rule_has_no_url(h: Hosts) -> None:
    app_id = new_id("app")
    with psycopg.connect(h.dsns.app) as conn:
        bind_org_sync(conn, h.org.org_id)
        conn.execute(
            "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, 'status', %s)",
            (app_id, h.org.org_id, h.org.admin_user_id),
        )
        for name in ("prod", "preview"):
            conn.execute(
                "insert into ssc.environment (id, org_id, app_id, name) values (%s, %s, %s, %s)",
                (new_id("env"), h.org.org_id, app_id, name),
            )
    try:
        set_cell_label(h, LABEL)
        r = h.client.get(f"/v1/apps/{app_id}", headers=auth(h.token))
        assert r.status_code == 200, r.text
        assert urls(r.json()) == {"prod": None, "preview": None}
    finally:
        set_cell_label(h, None)


# ── the setting ──────────────────────────────────────────────────────────────

_ENV = {"SSC_DATABASE_DSN": "postgresql://x@y/z", "SSC_API_JWKS": '{"keys": []}'}


def test_apps_domain_comes_from_ssc_apps_domain() -> None:
    base = {**_ENV, "SSC_API_ISSUER": ISSUER}
    assert Settings.from_env(base).apps_domain == "delimitusapps.com"
    assert Settings.from_env({**base, "SSC_APPS_DOMAIN": DOMAIN}).apps_domain == DOMAIN
    for bad in ("", "localhost", "Apps.Test", "apps.test.", "apps.test:8080"):
        with pytest.raises(ValueError, match="apps domain"):
            Settings.from_env({**base, "SSC_APPS_DOMAIN": bad})
    with pytest.raises(ValueError, match="apps domain"):
        Settings(database_dsn="x", jwks={"keys": []}, issuer=ISSUER, apps_domain="bad")
