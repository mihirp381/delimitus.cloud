"""Decision 019 amendment 2026-10-26: index the roster's last sign-in lookup.

Revision ID: 0043_roster_sign_in_index
Revises: 0042_pilot_invite
"""

from pathlib import Path

from alembic import context, op

revision = "0043_roster_sign_in_index"
down_revision = "0042_pilot_invite"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def _run(sql: str) -> None:
    if context.is_offline_mode():
        op.execute(sql)
        return
    dbapi = op.get_bind().connection.dbapi_connection
    if dbapi is None:
        raise RuntimeError("no DB-API connection behind the Alembic bind")
    dbapi.cursor().execute(sql)


def upgrade() -> None:
    _run((SQL_DIR / "0043_roster_sign_in_index.sql").read_text())


def downgrade() -> None:
    _run("DROP INDEX ssc.auth_session_last_sign_in;")
