"""Snapshot one WorkOS directory: users, emails, idp_id, state, group memberships."""

from __future__ import annotations

from typing import Any

from loginproof import api, config, store


def user_record(u: dict[str, Any], groups: list[dict[str, Any]]) -> dict[str, Any]:
    emails = [e.get("value", "").lower() for e in u.get("emails") or [] if e.get("value")]
    if u.get("email") and u["email"].lower() not in emails:
        emails.insert(0, u["email"].lower())
    return {
        "id": u.get("id"),
        "idp_id": u.get("idp_id"),
        "username": u.get("username"),
        "state": u.get("state"),
        "emails": emails,
        "groups": sorted(g.get("name", "") for g in groups),
        "group_idp_ids": sorted(g.get("idp_id", "") for g in groups),
        "raw_attributes": u.get("raw_attributes") or {},
        "updated_at": u.get("updated_at"),
    }


def snapshot(provider: str) -> dict[str, Any]:
    directory = config.directory_id(provider)
    users = api.list_directory_users(directory)
    groups = api.list_directory_groups(directory)
    recs = [user_record(u, api.list_groups_for_user(u["id"])) for u in users]
    return {
        "directory_id": directory,
        "users": recs,
        "groups": [{"id": g.get("id"), "idp_id": g.get("idp_id"), "name": g.get("name")} for g in groups],
    }


def main(providers: list[str] | None = None) -> None:
    for p in providers or config.configured_directory_providers():
        snap = snapshot(p)
        store.set_directory(p, snap)
        multi = [u["emails"][0] for u in snap["users"] if len(u["groups"]) >= 2]
        print(f"{p}: {len(snap['users'])} users, {len(snap['groups'])} groups, in >=2 groups: {multi}")
