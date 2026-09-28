"""Thin WorkOS REST client. Endpoints from workos.com/docs/reference (see NOTES.md)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx2

from loginproof import config


def _client() -> httpx2.Client:
    return httpx2.Client(base_url=config.API_BASE, timeout=30.0, headers={"Authorization": f"Bearer {config.api_key()}"})


def authorize_url(connection: str, state: str) -> str:
    q = {
        "client_id": config.client_id(),
        "redirect_uri": config.REDIRECT_URI,
        "response_type": "code",
        "connection": connection,
        "state": state,
    }
    return f"{config.API_BASE}/sso/authorize?{urlencode(q)}"


def exchange_code(code: str) -> dict[str, Any]:
    body = {
        "client_id": config.client_id(),
        "client_secret": config.api_key(),
        "grant_type": "authorization_code",
        "code": code,
    }
    with httpx2.Client(base_url=config.API_BASE, timeout=30.0) as c:
        r = c.post("/sso/token", json=body)
    r.raise_for_status()
    return r.json()


def _paginate(path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    after: str | None = None
    with _client() as c:
        while True:
            q = {**params, "limit": 100}
            if after:
                q["after"] = after
            r = c.get(path, params=q)
            r.raise_for_status()
            page = r.json()
            out.extend(page.get("data", []))
            after = (page.get("list_metadata") or {}).get("after")
            if not after:
                return out


def list_directory_users(directory: str) -> list[dict[str, Any]]:
    return _paginate("/directory_users", {"directory": directory})


def list_directory_groups(directory: str) -> list[dict[str, Any]]:
    return _paginate("/directory_groups", {"directory": directory})


def list_groups_for_user(user_id: str) -> list[dict[str, Any]]:
    return _paginate("/directory_groups", {"user": user_id})


def device_authorize() -> dict[str, Any]:
    with httpx2.Client(base_url=config.API_BASE, timeout=30.0) as c:
        r = c.post("/user_management/authorize/device", data={"client_id": config.client_id()})
    if r.status_code == 404:
        return {"not_offered": True, "http": 404, "body": r.text[:300]}
    r.raise_for_status()
    return r.json()


def device_poll(device_code: str) -> tuple[int, dict[str, Any]]:
    body = {
        "client_id": config.client_id(),
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
    }
    with httpx2.Client(base_url=config.API_BASE, timeout=30.0) as c:
        r = c.post("/user_management/authenticate", json=body)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.text[:300]}


def refresh(refresh_token: str) -> tuple[int, dict[str, Any]]:
    body = {"client_id": config.client_id(), "grant_type": "refresh_token", "refresh_token": refresh_token}
    with httpx2.Client(base_url=config.API_BASE, timeout=30.0) as c:
        r = c.post("/user_management/authenticate", json=body)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.text[:300]}


def list_sessions(user_id: str) -> list[dict[str, Any]]:
    with _client() as c:
        r = c.get(f"/user_management/users/{user_id}/sessions")
    r.raise_for_status()
    page = r.json()
    return page.get("data", page if isinstance(page, list) else [])


def revoke_session(session_id: str) -> int:
    with _client() as c:
        r = c.post("/user_management/sessions/revoke", json={"session_id": session_id})
    return r.status_code
