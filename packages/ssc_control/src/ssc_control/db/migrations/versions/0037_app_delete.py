"""GA-12 step 0.5: removing an app. ``app.status`` gains ``deleting`` and ``deleted``, a deleted
app's slug is free again (``app_slug_live`` in place of the unique constraint),
``kill_switch_run.mode`` gains ``delete``, and the app role may delete an ``app_database`` row.

Revision ID: 0037_app_delete
Revises: 0036_ci_tokens

Downgrade restores the vocabularies ``NOT VALID`` and the unique constraint (development
databases only): a deleted app or a removal run already written stays, and two apps that share a
slug, one of them deleted, refuse the constraint until one is renamed.
"""

from pathlib import Path

from alembic import context, op

revision = "0037_app_delete"
down_revision = "0036_ci_tokens"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
REVOKE DELETE ON ssc.app_database FROM ssc_app;
ALTER TABLE ssc.kill_switch_run
  DROP CONSTRAINT kill_switch_run_mode_check,
  ADD CONSTRAINT kill_switch_run_mode_check CHECK (mode IN ('disable', 'quarantine')) NOT VALID;
DROP INDEX ssc.app_slug_live;
ALTER TABLE ssc.app
  ADD CONSTRAINT app_org_id_slug_key UNIQUE (org_id, slug),
  DROP CONSTRAINT app_status_check,
  ADD CONSTRAINT app_status_check
    CHECK (status IN ('active', 'disabled', 'quarantined')) NOT VALID;
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
    _run((SQL_DIR / "0037_app_delete.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
