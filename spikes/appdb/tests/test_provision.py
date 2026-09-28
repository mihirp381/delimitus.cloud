from __future__ import annotations

import os
import pathlib

import psycopg
import pytest

from appdb.provision import create_app_database, drop_app_database

HERE = pathlib.Path(__file__).resolve().parent.parent
CA = str(HERE / "docker/certs/ca.crt")
WRONG_CA = str(HERE / "docker/certs/wrong-ca.crt")
HOST, PORT = "localhost", 55418
ADMIN_URL = os.environ.get(
    "ADMIN_URL", f"postgresql://ssc_admin:adminpw@{HOST}:{PORT}/postgres?sslmode=verify-full&sslrootcert={CA}"
)
IDS = [f"t{i:02d}" for i in range(10)]


@pytest.fixture(scope="module")
def apps():
    admin = psycopg.connect(ADMIN_URL)
    for i in IDS:
        drop_app_database(admin, i)
    creds = [create_app_database(admin, i, host=HOST, port=PORT, ca_path=CA) for i in IDS]
    yield creds
    for i in IDS:
        drop_app_database(admin, i)
    admin.close()


def test_each_role_connects_to_own_db_verify_full(apps):
    for c in apps:
        with psycopg.connect(c.database_url) as conn:
            assert conn.execute("select current_user, current_database(), ssl from pg_stat_ssl s join pg_stat_activity a using (pid) where a.pid = pg_backend_pid()").fetchone() == (c.role, c.database, True)
            conn.execute("create table t(x int)")
            conn.execute("insert into t values (1)")
            assert conn.execute("select count(*) from t").fetchone() == (1,)


def test_cross_connect_refused(apps):
    a, b = apps[0], apps[1]
    url = b.database_url.replace(f"//{b.role}:", f"//{a.role}:").replace(b.password, a.password)
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        psycopg.connect(url)


def test_cross_connect_all_pairs(apps):
    for a in apps:
        for b in apps:
            if a is b:
                continue
            url = b.database_url.replace(f"//{b.role}:", f"//{a.role}:").replace(b.password, a.password)
            with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
                psycopg.connect(url)


def test_app_role_cannot_create_db_or_role(apps):
    with psycopg.connect(apps[0].database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("create database evil")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("create role evil")


def test_wrong_ca_rejected(apps):
    url = apps[0].database_url.replace(CA, WRONG_CA)
    with pytest.raises(psycopg.OperationalError, match="certificate verify failed"):
        psycopg.connect(url)


def test_plain_connection_refused(apps):
    url = apps[0].database_url.split("?")[0] + "?sslmode=disable"
    with pytest.raises(psycopg.OperationalError, match="no pg_hba.conf entry|no encryption"):
        psycopg.connect(url)


def test_url_form(apps):
    c = apps[0]
    assert c.database_url.startswith(f"postgresql://{c.role}:")
    assert c.database_url.endswith(f"/{c.database}?sslmode=verify-full&sslrootcert={CA}")
