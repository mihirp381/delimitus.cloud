"""CLI login through the AuthKit device authorization flow, then the revocation check.

The refresh token stays in process memory. Default mode: obtain it, wait for the founder to deactivate the
user, press Enter, re-check. `--recheck` mode: read LOGINPROOF_REFRESH_TOKEN from the environment for a
separate run (never from disk), re-check, record the verdict.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from loginproof import api, store


def obtain() -> tuple[str | None, dict[str, Any]]:
    start = api.device_authorize()
    if start.get("not_offered"):
        store.set_device("flow", {"offered": False, "http": start["http"]})
        print("device authorization flow not offered by this environment (HTTP 404)")
        return None, {}
    print(f"open {start['verification_uri_complete']}")
    print(f"or enter code {start['user_code']} at {start['verification_uri']}")
    interval = int(start.get("interval", 5))
    deadline = time.monotonic() + int(start.get("expires_in", 300))
    while time.monotonic() < deadline:
        time.sleep(interval)
        status, body = api.device_poll(start["device_code"])
        if status == 200 and "refresh_token" in body:
            user = body.get("user") or {}
            rec = {
                "offered": True,
                "user_id": user.get("id"),
                "email": (user.get("email") or "").lower(),
                "organization_id": body.get("organization_id"),
                "authentication_method": body.get("authentication_method"),
                "access_token_received": bool(body.get("access_token")),
                "refresh_token_received": True,
            }
            store.set_device("flow", rec)
            print(f"device login done for {rec['email']}")
            return body["refresh_token"], rec
        err = body.get("error") or body.get("code")
        if err == "slow_down":
            interval += 5
        elif err in ("access_denied", "expired_token"):
            store.set_device("flow", {"offered": True, "error": err})
            print(f"device flow ended: {err}")
            return None, {}
        elif err != "authorization_pending":
            print(f"unexpected poll response {status}: {str(body)[:200]}")
    store.set_device("flow", {"offered": True, "error": "timeout"})
    return None, {}


def recheck(refresh_token: str, user_id: str | None) -> dict[str, Any]:
    status, body = api.refresh(refresh_token)
    verdict: dict[str, Any] = {
        "refresh_http": status,
        "refresh_error": body.get("error") or body.get("code"),
        "refresh_succeeded": status == 200 and "access_token" in body,
    }
    if user_id:
        try:
            sessions = api.list_sessions(user_id)
            verdict["sessions"] = [{"status": s.get("status"), "ended_at": s.get("ended_at")} for s in sessions]
        except Exception as e:  # noqa: BLE001
            verdict["sessions_error"] = f"{type(e).__name__}"
    verdict["token_revoked_on_deactivation"] = not verdict["refresh_succeeded"]
    return verdict


def main(argv: list[str]) -> None:
    if "--recheck" in argv:
        token = os.environ.get("LOGINPROOF_REFRESH_TOKEN")
        if not token:
            sys.exit("set LOGINPROOF_REFRESH_TOKEN for this run (export in the shell, never a .env file)")
        user_id = store.load().get("device", {}).get("flow", {}).get("user_id")
        verdict = recheck(token, user_id)
        store.set_device("recheck", verdict)
        print(verdict)
        return
    token, rec = obtain()
    if not token:
        return
    before = recheck(token, rec.get("user_id"))
    store.set_device("before_deactivation", before)
    print(f"refresh before deactivation: ok={before['refresh_succeeded']}")
    input("now deactivate this user in the identity provider AND wait for the directory sync, then press Enter ")
    after = recheck(token, rec.get("user_id"))
    store.set_device("recheck", after)
    print(f"refresh after deactivation: ok={after['refresh_succeeded']} -> revoked={after['token_revoked_on_deactivation']}")
