"""The audit log (SSC-012, decision 012): an append-only, hash-chained record per org.

:mod:`.chain` appends inside the caller's transaction, :mod:`.views` limits what a row may say,
:mod:`.verify` walks the chain, :mod:`.search` and :mod:`.export` read it back, and
``python -m ssc_control.audit verify --org <id>`` runs the verifier from a shell.
"""

from ssc_control.audit.chain import (
    GENESIS_HASH,
    HASH_LENGTH,
    Actor,
    AppendedEvent,
    NewEvent,
    append_event,
    canonical_bytes,
)
from ssc_control.audit.views import VIEWS, AuditViewError, check_view

__all__ = [
    "GENESIS_HASH",
    "HASH_LENGTH",
    "VIEWS",
    "Actor",
    "AppendedEvent",
    "AuditViewError",
    "NewEvent",
    "append_event",
    "canonical_bytes",
    "check_view",
]
