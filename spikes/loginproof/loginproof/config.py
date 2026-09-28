from __future__ import annotations

import os
from pathlib import Path

API_BASE = os.environ.get("WORKOS_API_BASE", "https://api.workos.com")
OUT_DIR = Path(os.environ.get("LOGINPROOF_OUT", Path(__file__).resolve().parent.parent / "out"))
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8765
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"

SSO_PROVIDERS = ("GOOGLE", "ENTRA_OIDC", "ENTRA_SAML", "OKTA")
DIRECTORY_PROVIDERS = ("GOOGLE", "ENTRA", "OKTA")
SSO_TO_DIRECTORY = {"GOOGLE": "GOOGLE", "ENTRA_OIDC": "ENTRA", "ENTRA_SAML": "ENTRA", "OKTA": "OKTA"}


class MissingEnv(RuntimeError):
    pass


def require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingEnv(f"set {name} in the environment (export it in the shell; never source a .env file)")
    return value


def api_key() -> str:
    return require("WORKOS_API_KEY")


def client_id() -> str:
    return require("WORKOS_CLIENT_ID")


def connection_id(provider: str) -> str:
    return require(f"WORKOS_CONN_{provider}")


def directory_id(provider: str) -> str:
    return require(f"WORKOS_DIR_{provider}")


def configured_sso_providers() -> list[str]:
    return [p for p in SSO_PROVIDERS if os.environ.get(f"WORKOS_CONN_{p}")]


def configured_directory_providers() -> list[str]:
    return [p for p in DIRECTORY_PROVIDERS if os.environ.get(f"WORKOS_DIR_{p}")]
