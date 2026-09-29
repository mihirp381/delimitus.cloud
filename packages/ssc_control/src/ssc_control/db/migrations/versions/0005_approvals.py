"""SSC-045 approvals: ``environment.profile`` and the ``approval_request`` columns and checks.

Revision ID: 0005_approvals
Revises: 0003_lane_vocab

Expand step (see the SQL file). Downgrade drops the new columns, which discards every approval
request's environment and decision details; it exists for development databases.
"""

from pathlib import Path

from alembic import context, op

revision = "0005_approvals"
down_revision = "0003_lane_vocab"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
DROP INDEX ssc.approval_request_created_idx;
ALTER TABLE ssc.approval_request
  DROP CONSTRAINT approval_request_not_self,
  DROP CONSTRAINT approval_request_kind_check,
  DROP CONSTRAINT approval_request_reason_check,
  DROP CONSTRAINT approval_request_pending_undecided_check,
  DROP COLUMN policy_decision_id,
  DROP COLUMN recorded_by_operator,
  DROP COLUMN decision_channel,
  DROP COLUMN decision_reason,
  DROP COLUMN subject_key,
  DROP COLUMN environment_id,
  ADD CHECK (state <> 'approved' OR decided_by_user_id IS DISTINCT FROM requested_by_user_id);
ALTER TABLE ssc.environment DROP COLUMN profile;
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
    _run((SQL_DIR / "0005_approvals.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
