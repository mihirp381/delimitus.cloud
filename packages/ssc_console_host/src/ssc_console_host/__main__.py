"""``python -m ssc_console_host``: console.delimitus.com on Cloud Run (SSC gap 1)."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import uvicorn

from ssc_console_host.app import create_app
from ssc_console_host.site import load_site

DEV_ENVS: Final = frozenset({"dev", "test"})
ORIGIN: Final = re.compile(r"(https?)://[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?")
"""Scheme and host, an optional port; nothing that could add to the policy it is written into."""


class SettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    dist: Path
    auth_origin: str
    port: int


def settings_from_env(env: Mapping[str, str]) -> Settings:
    environment = env.get("SSC_CONSOLE_ENV", "prod")
    origin = env.get("SSC_CONSOLE_AUTH_ORIGIN", "")
    if not origin:
        raise SettingsError("SSC_CONSOLE_AUTH_ORIGIN is required")
    matched = ORIGIN.fullmatch(origin)
    if matched is None:
        raise SettingsError("SSC_CONSOLE_AUTH_ORIGIN is an origin: scheme and host, no path")
    if environment not in DEV_ENVS and matched[1] != "https":
        raise SettingsError("SSC_CONSOLE_AUTH_ORIGIN must be https outside dev and test")
    dist = env.get("SSC_CONSOLE_DIST", "")
    if not dist:
        raise SettingsError("SSC_CONSOLE_DIST is required")
    try:
        port = int(env.get("PORT", "8080"))
    except ValueError as exc:
        raise SettingsError("PORT is a number") from exc
    return Settings(dist=Path(dist), auth_origin=origin, port=port)


def main() -> None:
    settings = settings_from_env(os.environ)
    uvicorn.run(
        create_app(load_site(settings.dist), auth_origin=settings.auth_origin),
        host="0.0.0.0",  # noqa: S104  (Cloud Run routes to the container's port)
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
        date_header=True,
    )


if __name__ == "__main__":
    main()
