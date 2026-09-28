"""Match SSO login records to directory users and judge the join key per provider.

Pure functions over the records store so the logic is unit-testable with recorded-shape fixtures.
"""

from __future__ import annotations

from typing import Any

from loginproof.config import SSO_TO_DIRECTORY

NOT_MEASURED = "not measured"


def _flatten(obj: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    elif obj is not None:
        out[prefix] = str(obj)
    return out


def match_login(login: dict[str, Any], users: list[dict[str, Any]]) -> dict[str, Any]:
    """Return {"user": rec|None, "key": how it matched}.

    Order: SSO idp_id == directory idp_id; an identifier-like login claim (oid/sub/objectidentifier/id/
    externalId) equal to the directory idp_id; a non-email SSO idp_id found inside directory raw_attributes
    (names the attribute); email as the fallback.
    """
    idp = login.get("idp_id")
    if idp:
        for u in users:
            if u.get("idp_id") == idp:
                return {"user": u, "key": "idp_id"}
    login_ids = {
        k: v
        for k, v in _flatten(login.get("raw_attributes")).items()
        if k.rsplit(".", 1)[-1].lower() in {"oid", "sub", "objectidentifier", "id", "externalid"}
        or k.lower().endswith("/objectidentifier")
    }
    for u in users:
        if u.get("idp_id") and u["idp_id"] in login_ids.values():
            path = next(k for k, v in login_ids.items() if v == u["idp_id"])
            return {"user": u, "key": f"login.raw_attributes.{path} == directory.idp_id"}
    if idp and "@" not in idp:
        for u in users:
            for path, val in _flatten(u.get("raw_attributes")).items():
                if val == idp:
                    return {"user": u, "key": f"directory.raw_attributes.{path}"}
    email = (login.get("email") or "").lower()
    if email:
        for u in users:
            if email in [e.lower() for e in u.get("emails", [])]:
                return {"user": u, "key": "email"}
    return {"user": None, "key": None}


def stability(logins: list[dict[str, Any]]) -> str | bool:
    """True when one idp_id appears with two different emails. False when the same person (same directory
    match) got a new idp_id after the email change. Not measured otherwise."""
    by_idp: dict[str, set[str]] = {}
    for rec in logins:
        if rec.get("idp_id"):
            by_idp.setdefault(rec["idp_id"], set()).add(rec.get("email", ""))
    if any(len(v) >= 2 for v in by_idp.values()):
        return True
    by_user: dict[str, set[str]] = {}
    for rec in logins:
        if rec.get("matched_user_id"):
            by_user.setdefault(rec["matched_user_id"], set()).add(rec.get("idp_id") or "")
    if any(len(v) >= 2 for v in by_user.values()):
        return False
    return NOT_MEASURED


def evaluate(records: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    logins_by_provider: dict[str, list[dict[str, Any]]] = {}
    for rec in records.get("logins", []):
        logins_by_provider.setdefault(rec["provider"], []).append(rec)
    for provider, logins in logins_by_provider.items():
        dir_provider = SSO_TO_DIRECTORY.get(provider, provider)
        users = records.get("directories", {}).get(dir_provider, {}).get("users", [])
        keys: list[str] = []
        matched = 0
        annotated: list[dict[str, Any]] = []
        for rec in logins:
            m = match_login(rec, users)
            if m["user"]:
                matched += 1
                keys.append(m["key"])
                annotated.append({**rec, "matched_user_id": m["user"]["id"]})
            else:
                annotated.append(rec)
        key_set = sorted(set(keys))
        result[provider] = {
            "logins": len(logins),
            "directory_users": len(users),
            "matched": matched,
            "join_key": key_set[0] if len(key_set) == 1 else (key_set or None),
            "idp_id_present": all(bool(r.get("idp_id")) for r in logins),
            "stable_across_email_change": stability(annotated),
            "group_sharing_possible": bool(users) and matched == len(logins) and key_set not in ([], ["email"]),
        }
    return result


def main() -> None:
    from loginproof import store

    for provider, r in evaluate(store.load()).items():
        print(provider, r)
