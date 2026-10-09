"""GA-7.7: CI tokens. ``auth_session`` gains the ``ci`` kind (up to 90 days, every other kind
keeps 12 hours), ``scope`` and ``label`` (both set exactly for ``ci``) and the ``revoked`` revoke
reason.

Revision ID: 0036_ci_tokens
Revises: 0035_app_database_host

Downgrade drops the columns and restores the vocabularies and the 12-hour lifetime ``NOT VALID``
(development databases only): a ``ci`` row already written stays, a later update of it fails the
restored checks, and upgrading again refuses it (it has lost its scope and label) until it is
deleted.
"""

from pathlib import Path

from alembic import context, op

revision = "0036_ci_tokens"
down_revision = "0035_app_database_host"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.auth_session
  DROP CONSTRAINT auth_session_ci_check,
  DROP CONSTRAINT auth_session_lifetime_check,
  DROP COLUMN scope,
  DROP COLUMN label,
  DROP CONSTRAINT auth_session_kind_check,
  ADD CONSTRAINT auth_session_kind_check CHECK (kind IN ('browser', 'cli', 'console')) NOT VALID,
  DROP CONSTRAINT auth_session_revoke_reason_check,
  ADD CONSTRAINT auth_session_revoke_reason_check CHECK (revoke_reason IN (
    'logout', 'user_deactivated', 'refresh_reuse', 'operator', 'code_reuse')) NOT VALID,
  ADD CONSTRAINT auth_session_check
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '12 hours') NOT VALID;
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
    _run((SQL_DIR / "0036_ci_tokens.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
