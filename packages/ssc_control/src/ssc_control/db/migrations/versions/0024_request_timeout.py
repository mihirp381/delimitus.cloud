"""SSC-090 request deadline: ``environment.request_timeout_seconds``, how long Cloud Run lets one
request to the environment run, as the gateway tells the app. NULL means the request-billed
figure.

Revision ID: 0024_request_timeout
Revises: 0023_usage_metrics

Downgrade drops the column (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0024_request_timeout"
down_revision = "0023_usage_metrics"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.environment DROP COLUMN request_timeout_seconds;
"""


def _run(sql: str) -> None:
    if context.is_offline_mode():
        op.execute(sql)
        return
    dbapi = op.get_bind().connection.dbapi_connection
    if dbapi is None:
        raise RuntimeError("no DB-API connection behind the Alembic bind")
    dbapi.cursor().execute(sql)


def upgrade() -> None:
    _run((SQL_DIR / "0024_request_timeout.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
