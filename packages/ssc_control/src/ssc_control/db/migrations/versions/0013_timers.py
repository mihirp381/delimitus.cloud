"""SSC-041 timers: what a schedule calls and when it runs next, and ``ssc.timer_run``.

Revision ID: 0013_timers
Revises: 0012_kill_switch

Downgrade deletes every schedule and its runs, drops the new columns and restores the plain
unique name (development databases only). Upgrading needs an empty ``ssc.schedule``, as it was
before this revision: ``path`` and ``declared_by_user_id`` are NOT NULL without a default.
"""

from pathlib import Path

from alembic import context, op

revision = "0013_timers"
down_revision = "0012_kill_switch"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP TABLE ssc.timer_run;
ALTER TABLE ssc.schedule NO FORCE ROW LEVEL SECURITY;
DELETE FROM ssc.schedule;
ALTER TABLE ssc.schedule FORCE ROW LEVEL SECURITY;
DROP INDEX ssc.schedule_armed;
DROP INDEX ssc.schedule_live_name;
ALTER TABLE ssc.schedule
  DROP CONSTRAINT schedule_declared_by_fk,
  DROP CONSTRAINT schedule_active_is_armed,
  DROP CONSTRAINT schedule_paused_has_reason,
  DROP COLUMN declared_by_user_id,
  DROP COLUMN last_scheduled_for,
  DROP COLUMN next_run_at,
  DROP COLUMN pause_reason,
  DROP COLUMN timeout_seconds,
  DROP COLUMN method,
  DROP COLUMN path,
  ADD CONSTRAINT schedule_org_id_environment_id_name_key UNIQUE (org_id, environment_id, name);
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
    _run((SQL_DIR / "0013_timers.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
