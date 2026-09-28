"""Callback receiver. `/login?provider=OKTA` -> WorkOS -> `/callback` records the SSO profile."""

from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse, RedirectResponse

from loginproof import api, config, store

app = FastAPI(title="loginproof")


def profile_record(provider: str, profile: dict) -> dict:
    return {
        "provider": provider,
        "profile_id": profile.get("id"),
        "idp_id": profile.get("idp_id"),
        "connection_id": profile.get("connection_id"),
        "connection_type": profile.get("connection_type"),
        "organization_id": profile.get("organization_id"),
        "email": (profile.get("email") or "").lower(),
        "first_name": profile.get("first_name"),
        "last_name": profile.get("last_name"),
        "groups": profile.get("groups") or [],
        "raw_attribute_keys": sorted((profile.get("raw_attributes") or {}).keys()),
        "raw_attributes": profile.get("raw_attributes") or {},
    }


@app.get("/")
def index() -> PlainTextResponse:
    lines = ["loginproof callback server", ""]
    for p in config.configured_sso_providers():
        lines.append(f"http://{config.CALLBACK_HOST}:{config.CALLBACK_PORT}/login?provider={p}")
    if len(lines) == 2:
        lines.append("no WORKOS_CONN_* variables set")
    return PlainTextResponse("\n".join(lines))


@app.get("/login")
def login(provider: str = Query(...)) -> RedirectResponse | PlainTextResponse:
    if provider not in config.SSO_PROVIDERS:
        return PlainTextResponse(f"unknown provider {provider}", status_code=400)
    try:
        return RedirectResponse(api.authorize_url(config.connection_id(provider), state=provider))
    except config.MissingEnv as e:
        return PlainTextResponse(str(e), status_code=400)


@app.get("/callback")
def callback(code: str = Query(...), state: str = Query("")) -> PlainTextResponse:
    token = api.exchange_code(code)
    rec = profile_record(state, token["profile"])
    store.add_login(rec)
    body = "\n".join(
        [
            f"recorded login for {state}",
            f"connection_type={rec['connection_type']}",
            f"idp_id={rec['idp_id']}",
            f"email={rec['email']}",
            f"groups={rec['groups']}",
            f"raw_attribute_keys={rec['raw_attribute_keys']}",
            "",
            "close this tab and continue with the README checklist",
        ]
    )
    return PlainTextResponse(body)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.CALLBACK_HOST, port=config.CALLBACK_PORT, log_level="warning")
