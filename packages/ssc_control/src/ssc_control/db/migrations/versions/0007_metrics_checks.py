"""SSC-028 metrics events: CHECKs on the ``metrics_event`` pseudonym, tool, app id and properties.

Revision ID: 0007_metrics_checks
Revises: 0006_procrastinate_orgindex

Downgrade drops the four constraints; no data changes either way.
"""

from pathlib import Path

from alembic import context, op

revision = "0007_metrics_checks"
down_revision = "0006_procrastinate_orgindex"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.metrics_event
  DROP CONSTRAINT metrics_event_properties_check,
  DROP CONSTRAINT metrics_event_app_id_check,
  DROP CONSTRAINT metrics_event_source_tool_check,
  DROP CONSTRAINT metrics_event_pseudonym_check;
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
    _run((SQL_DIR / "0007_metrics_checks.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
