"""SSC-028 usage events: session hours, instance hours, cold starts and fixed cell resources.
Counts and durations only, never request content, paths, user ids or IP addresses; for metrics
and the cost view, never for charging (A6).

``metrics_event`` gains ``environment_id`` and ``dedup_key``. A unique index on ``(org_id, kind,
dedup_key)`` makes an event with a key one of a kind, so a collector run twice or a retried job
writes nothing new. Neither column references another row: metrics outlive what they describe.
``usage_collection`` holds one row per org (one org is one cell): the end of the last whole hour
collected, moved forward in the transaction that writes that hour's events.

Revision ID: 0023_usage_metrics
Revises: 0022_app_databases

Downgrade drops the new columns, indexes and table, and puts back the old list of kinds unchecked
against existing rows (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0023_usage_metrics"
down_revision = "0022_app_databases"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.usage_collection;
DROP INDEX ssc.metrics_event_environment_at;
DROP INDEX ssc.metrics_event_once;
ALTER TABLE ssc.metrics_event DROP CONSTRAINT metrics_event_kind_check;
ALTER TABLE ssc.metrics_event ADD CONSTRAINT metrics_event_kind_check CHECK (kind IN (
  'first_url', 'deploy', 'share', 'app_opened', 'data_query', 'database_use', 'timer_run'))
  NOT VALID;
ALTER TABLE ssc.metrics_event DROP COLUMN dedup_key, DROP COLUMN environment_id;
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
    _run((SQL_DIR / "0023_usage_metrics.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
