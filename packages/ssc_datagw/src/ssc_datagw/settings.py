"""What the data gateway reads from its environment (SSC-050). Infra sets each of these on the
``ssc-datagw`` service (``infra/ssc_infra/cell.py``, ``_datagw_env``)."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from ssc_shared.hosts import check_apps_domain, check_cell_label

MAX_STALE_SECONDS: Final = 120.0
"""The snapshot age past which every query is refused with ``DATA_SNAPSHOT_STALE``."""
_ORG = re.compile(r"org_[a-z0-9]{20}")
_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")


class SettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    """``audience`` is the URL apps mint their workload token for: the service's own
    ``run.app`` URL. ``project_id`` is the cell project, whose ``ssc-a-*`` accounts are apps.
    ``jwks`` is the cell's public identity JWKS, which signs the identity notes apps forward."""

    org_id: str
    cell_label: str
    project_id: str
    bucket: str
    audience: str
    jwks: Mapping[str, Any]
    issuer: str
    apps_domain: str
    max_stale: float = MAX_STALE_SECONDS


def _need(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise SettingsError(f"{name} is required")
    return value


def _jwks(raw: str) -> Mapping[str, Any]:
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise SettingsError("SSC_IDENTITY_JWKS is not JSON") from exc
    keys = cast("dict[str, object]", doc).get("keys") if isinstance(doc, dict) else None
    if not isinstance(keys, list) or not keys:
        raise SettingsError("SSC_IDENTITY_JWKS needs a non-empty keys list")
    if any(not isinstance(k, dict) or "d" in k for k in cast("list[object]", keys)):
        raise SettingsError("SSC_IDENTITY_JWKS holds public keys only")
    return cast("dict[str, Any]", doc)


def settings_from_env(env: Mapping[str, str]) -> Settings:
    org_id = _need(env, "SSC_ORG_ID")
    if _ORG.fullmatch(org_id) is None:
        raise SettingsError("SSC_ORG_ID must be org_ followed by 20 lowercase letters or digits")
    project_id = _need(env, "SSC_PROJECT_ID")
    if _PROJECT.fullmatch(project_id) is None:
        raise SettingsError("SSC_PROJECT_ID is not a project id")
    audience = _need(env, "SSC_DATAGW_AUDIENCE").rstrip("/")
    if not audience.startswith("https://"):
        raise SettingsError("SSC_DATAGW_AUDIENCE must be an https URL")
    try:
        label = check_cell_label(_need(env, "SSC_CELL_LABEL"))
        apps_domain = check_apps_domain(env.get("SSC_APPS_DOMAIN", "delimitusapps.com"))
        max_stale = float(env.get("SSC_SNAPSHOT_MAX_AGE", MAX_STALE_SECONDS))
    except ValueError as exc:
        raise SettingsError(str(exc)) from exc
    if not 0 < max_stale <= MAX_STALE_SECONDS:
        raise SettingsError(f"SSC_SNAPSHOT_MAX_AGE is above 0 and at most {MAX_STALE_SECONDS:g}")
    return Settings(
        org_id=org_id,
        cell_label=label,
        project_id=project_id,
        bucket=_need(env, "SSC_CELL_BUCKET"),
        audience=audience,
        jwks=_jwks(_need(env, "SSC_IDENTITY_JWKS")),
        issuer=env.get("SSC_IDENTITY_ISSUER", f"https://keys.delimitus.com/{label}"),
        apps_domain=apps_domain,
        max_stale=max_stale,
    )
