"""SSC-041 timer start: ``timer_run.start_ms``, how long the run's start request to the app's
``health_path`` took, and the ``start_failed`` error.

Revision ID: 0025_timer_start
Revises: 0024_request_timeout

Downgrade drops the column and records ``start_failed`` runs as ``dispatch_error``, with
``FORCE ROW LEVEL SECURITY`` lifted on ``timer_run`` inside the transaction, as in 0013
(development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0025_timer_start"
down_revision = "0024_request_timeout"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.timer_run NO FORCE ROW LEVEL SECURITY;
UPDATE ssc.timer_run SET error = 'dispatch_error' WHERE error = 'start_failed';
ALTER TABLE ssc.timer_run FORCE ROW LEVEL SECURITY;
ALTER TABLE ssc.timer_run
  DROP COLUMN start_ms,
  DROP CONSTRAINT timer_run_error_check,
  ADD CONSTRAINT timer_run_error_check CHECK (error IN (
    'overlap', 'app_inactive', 'owner_inactive', 'builder_access_revoked',
    'deleted', 'dispatch_unavailable', 'dispatch_error', 'http_error',
    'timeout', 'abandoned'));
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
    _run((SQL_DIR / "0025_timer_start.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
