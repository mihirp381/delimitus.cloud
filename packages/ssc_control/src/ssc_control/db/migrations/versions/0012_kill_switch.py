"""SSC-025 kill switch and inventory: ``ssc.kill_switch_run`` and ``metrics_event_last_used``.

Revision ID: 0012_kill_switch
Revises: 0011_audit_anchor

Expand step: a new table and a new partial index. Downgrade drops both (development databases
only).
"""

from pathlib import Path

from alembic import context, op

revision = "0012_kill_switch"
down_revision = "0011_audit_anchor"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP INDEX ssc.metrics_event_last_used;
DROP TABLE ssc.kill_switch_run;
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
    _run((SQL_DIR / "0012_kill_switch.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
