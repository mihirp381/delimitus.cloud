"""What an audit row may say about each kind of target.

``before`` and ``after`` hold only the keys listed for the row's ``target_kind``. The lists name
no personal data and no secret material, so the log never needs editing to honour an erasure
request. Values are strings, integers, booleans, null or lists of strings; there are no floats,
because their JSON spelling is not stable across languages. The one nested object is the
``filters`` of an ``audit`` row, and only the keys in :data:`FILTER_KEYS` may appear in it.

A new target kind needs an entry here before its first ``uow.audit()`` call.
"""

from collections.abc import Mapping
from typing import Any, Final, cast

VIEWS: Final[Mapping[str, frozenset[str]]] = {
    "app": frozenset({"slug", "owner_user_id", "status"}),
    "app_grant": frozenset({"environment_id", "role", "subject_kind", "subject_id"}),
    "secret_ref": frozenset({"environment_id", "name", "version"}),  # SSC-026: never a value
    "deployment": frozenset(
        {"kind", "state", "release_id", "environment_id", "failure_code", "superseded"}
    ),
    "build": frozenset(
        {
            "environment_id",
            "bundle_id",
            "state",
            "release_id",
            "failure_code",
            "via",
            "source_release_id",
        }
    ),
    "kill_switch_run": frozenset(
        {"app_id", "mode", "step", "state", "snapshot_version", "elapsed_ms", "attempts", "error"}
    ),
    "release": frozenset(
        {"number", "image_digest", "manifest_digest", "source_digest", "source_commit"}
    ),
    "user_account": frozenset({"role", "status"}),
    "user_group": frozenset({"directory_ref", "added", "removed"}),
    "environment": frozenset({"name", "profile", "grants_version"}),
    "approval_request": frozenset(
        {
            "kind",
            "environment_id",
            "subject_key",
            "state",
            "requested_by_user_id",
            "decided_by_user_id",
            "decision_channel",
        }
    ),
    "schedule": frozenset(
        {"name", "cron", "timezone", "path", "method", "timeout_seconds", "state", "pause_reason"}
        | {"environment_id", "run_id"}  # SSC-041
    ),
    "audit": frozenset({"format", "filters"}),
    "audit_anchor": frozenset({"restored_to", "prior_anchor_seq", "head_seq", "ref"}),
    "org": frozenset({"name"}),
    "auth_session": frozenset({"kind", "user_id", "reason", "via"}),  # SSC-019
    "directory_connection": frozenset(
        {"state", "reason", "join_rule", "connection_type", "workos_directory_id"}
    ),  # SSC-019
    "identity_link": frozenset({"user_id", "source", "unlinked_login_id"}),  # SSC-019
    "bundle": frozenset(
        {"app_id", "digest", "size_bytes", "file_count", "manifest_digest", "source_commit"}
    ),
    "cell_resource": frozenset(
        {"state", "cause", "attempts", "execution", "failure_code", "deployment_id"}
    ),  # SSC-087
}

FILTER_KEYS: Final = frozenset(
    {"since", "until", "action", "actor_kind", "actor_id", "target_kind", "target_id"}
)


class AuditViewError(ValueError):
    """An audit ``before`` or ``after`` that its target kind's view does not allow."""


def _scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int))


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in cast(list[object], value))


def _check_filters(value: object) -> None:
    if not isinstance(value, dict):
        raise AuditViewError("filters must be an object")
    for key, item in cast(dict[object, object], value).items():
        if key not in FILTER_KEYS:
            raise AuditViewError(f"filter {key!r} is not an audit filter")
        if not (_scalar(item) or _string_list(item)):
            raise AuditViewError(f"filter {key!r} must be a string, integer, boolean or null")


def check_view(target_kind: str, data: Mapping[str, Any] | None) -> None:
    """Raise :class:`AuditViewError` unless ``data`` fits the view for ``target_kind``."""
    if data is None:
        return
    allowed = VIEWS.get(target_kind)
    if allowed is None:
        raise AuditViewError(f"no audit view for target kind {target_kind!r}")
    for key, value in data.items():
        if key not in allowed:
            raise AuditViewError(f"{key!r} is not in the {target_kind!r} audit view")
        if key == "filters" and target_kind == "audit":
            _check_filters(value)
        elif not (_scalar(value) or _string_list(value)):
            raise AuditViewError(
                f"{target_kind}.{key} must be a string, integer, boolean, null or list of strings"
            )
