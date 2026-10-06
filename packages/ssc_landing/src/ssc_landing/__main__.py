"""``python -m ssc_landing``: the delimitus.com service on Cloud Run (SSC-065)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import uvicorn
from fastapi import FastAPI

from ssc_landing.app import create_app
from ssc_landing.limits import LOAD_BALANCER_HOPS
from ssc_landing.page import load_page
from ssc_landing.store import GcsPilotStore, MemoryPilotStore, PilotStore, bucket_named

DEV_ENVS: Final = frozenset({"dev", "test"})


class SettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    page: Path
    origin: str
    redirect_hosts: frozenset[str]
    bucket: str | None
    """None keeps requests in memory: dev and test only."""
    trusted_hops: int
    port: int


def settings_from_env(env: Mapping[str, str]) -> Settings:
    environment = env.get("SSC_LANDING_ENV", "prod")
    bucket = env.get("SSC_LANDING_BUCKET") or None
    if bucket is None and environment not in DEV_ENVS:
        raise SettingsError("SSC_LANDING_BUCKET is required outside SSC_LANDING_ENV dev or test")
    page = env.get("SSC_LANDING_PAGE", "")
    if not page:
        raise SettingsError("SSC_LANDING_PAGE is required")
    origin = env.get("SSC_LANDING_ORIGIN", "https://delimitus.com").rstrip("/")
    if environment not in DEV_ENVS and not origin.startswith("https://"):
        raise SettingsError("SSC_LANDING_ORIGIN must be https outside dev and test")
    hosts = env.get("SSC_LANDING_REDIRECT_HOSTS", "www.delimitus.com")
    try:
        hops = int(env.get("SSC_LANDING_TRUSTED_HOPS", str(LOAD_BALANCER_HOPS)))
        port = int(env.get("PORT", "8080"))
    except ValueError as exc:
        raise SettingsError("SSC_LANDING_TRUSTED_HOPS and PORT are numbers") from exc
    return Settings(
        page=Path(page),
        origin=origin,
        redirect_hosts=frozenset(h.strip().lower() for h in hosts.split(",") if h.strip()),
        bucket=bucket,
        trusted_hops=hops,
        port=port,
    )


def production_app(settings: Settings) -> FastAPI:
    store: PilotStore
    if settings.bucket is None:
        store = MemoryPilotStore()
    else:
        store = GcsPilotStore(bucket_named(settings.bucket))
    return create_app(
        load_page(settings.page),
        store,
        origin=settings.origin,
        redirect_hosts=settings.redirect_hosts,
        trusted_hops=settings.trusted_hops,
    )


def main() -> None:
    settings = settings_from_env(os.environ)
    # No access log: the form's answers are in bodies, but addresses are personal data too.
    uvicorn.run(
        production_app(settings),
        host="0.0.0.0",  # noqa: S104  (Cloud Run routes to the container's port)
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
        date_header=True,
    )


if __name__ == "__main__":
    main()
