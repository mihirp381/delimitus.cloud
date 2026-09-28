import psycopg
from testcontainers.postgres import PostgresContainer


def test_ci_postgres_is_version_18():
    with PostgresContainer("postgres:18", driver=None) as pg:
        with psycopg.connect(pg.get_connection_url()) as conn:
            major = conn.execute("show server_version_num").fetchone()[0]
    assert int(major) // 10000 == 18
