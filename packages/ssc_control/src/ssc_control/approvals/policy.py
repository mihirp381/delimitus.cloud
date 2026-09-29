"""Record one policy decision: who asked to do what to which target, the answer, and why.

Every approval decision and every production-gate outcome writes one ``ssc.policy_decision``
row in the caller's transaction; audit rows point at it through ``policy_decision_id``.
"""

import json
from collections.abc import Mapping
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.ids import new_id

PolicyPrincipalKind = Literal["user", "group", "workload", "schedule", "integration", "operator"]
PolicyOutcome = Literal["allow", "deny"]

_INSERT = text(
    "insert into ssc.policy_decision (id, org_id, principal_kind, principal_id, action, "
    "target_kind, target_id, outcome, reason, snapshot_version, inputs) values (:id, :org, "
    ":principal_kind, :principal_id, :action, :target_kind, :target_id, :outcome, :reason, "
    ":snapshot_version, cast(:inputs as jsonb))"
)


async def record_policy_decision(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    principal_kind: PolicyPrincipalKind,
    principal_id: str,
    action: str,
    target_kind: str,
    target_id: str,
    outcome: PolicyOutcome,
    reason: str,
    inputs: Mapping[str, object],
    snapshot_version: int | None = None,
) -> str:
    """Insert the row inside ``conn``'s org-bound transaction and return its ``pol_`` id."""
    pol_id = new_id("pol")
    await conn.execute(
        _INSERT,
        {
            "id": pol_id,
            "org": org_id,
            "principal_kind": principal_kind,
            "principal_id": principal_id,
            "action": action,
            "target_kind": target_kind,
            "target_id": target_id,
            "outcome": outcome,
            "reason": reason,
            "snapshot_version": snapshot_version,
            "inputs": json.dumps(dict(inputs), sort_keys=True, ensure_ascii=False),
        },
    )
    return pol_id
