"""SQLSTATE codes the control database raises, mirrored from the PL/pgSQL in the migrations.

Class ``SC`` is ours. On-call paging keys on ``SC001``: it means a code path queried the
control database without binding an org, which is a bug, never a normal outcome.
"""

from enum import StrEnum
from typing import Final


class SqlState(StrEnum):
    NO_ORG_BOUND = "SC001"
    LAST_ORG_ADMIN = "SC002"
    OWNER_NOT_ACTIVE = "SC003"
    RELEASE_IMMUTABLE = "SC004"
    AUDIT_IMMUTABLE = "SC005"
    TRUNCATE_REFUSED = "SC006"
    SCHEDULE_DELETED = "SC007"
    CELL_RESOURCE_READY = "SC008"


# Standard Postgres codes the tests and the API error catalogue translate.
INSUFFICIENT_PRIVILEGE: Final = "42501"  # also raised for a row-level security violation
NOT_NULL_VIOLATION: Final = "23502"
FOREIGN_KEY_VIOLATION: Final = "23503"
UNIQUE_VIOLATION: Final = "23505"
CHECK_VIOLATION: Final = "23514"
