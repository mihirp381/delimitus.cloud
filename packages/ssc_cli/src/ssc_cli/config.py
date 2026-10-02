"""Which API to talk to: ``--api``, then ``SSC_API_URL``, then the config file, then the default.

The config file is ``config.toml`` in Typer's per-user app directory for ``ssc`` and holds one key,
``api_url``. Plain ``http`` is refused except for loopback addresses, so a token is never sent
in clear text to another machine.
"""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import typer

from ssc_cli.errors import BAD_API_URL, BAD_CONFIG, ExitCode, local_error

DEFAULT_API_URL: Final = "https://api.delimitus.com"
ENV_API_URL: Final = "SSC_API_URL"
APP_DIR_NAME: Final = "ssc"
CONFIG_FILE: Final = "config.toml"
LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class Config:
    api_url: str


def config_path() -> Path:
    return Path(typer.get_app_dir(APP_DIR_NAME)) / CONFIG_FILE


def load_config(api_override: str | None = None, env: Mapping[str, str] | None = None) -> Config:
    e = os.environ if env is None else env
    raw = api_override or e.get(ENV_API_URL) or _from_file(config_path()) or DEFAULT_API_URL
    return Config(api_url=normalise_api_url(raw))


def normalise_api_url(raw: str, what: str = "API") -> str:
    url = raw.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises on a malformed port
    except ValueError:
        host, parts = None, None
    if (
        parts is None
        or parts.scheme not in {"https", "http"}
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise local_error(
            BAD_API_URL,
            f"The {what} address is not valid.",
            "Give an https:// address with no user name, query or fragment.",
            ExitCode.USAGE,
        )
    if parts.scheme == "http" and host not in LOOPBACK_HOSTS:
        raise local_error(
            BAD_API_URL,
            f"The {what} address must use https.",
            "Plain http is allowed only for localhost, so a token never crosses the network "
            "unencrypted.",
            ExitCode.USAGE,
        )
    return url


def _from_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise _bad_config(path) from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise _bad_config(path) from e
    value = data.get("api_url")
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bad_config(path)
    return value


def _bad_config(path: Path) -> Exception:
    return local_error(
        BAD_CONFIG,
        "The ssc config file cannot be read.",
        f'Fix or remove {path}. It holds one key: api_url = "https://...".',
        ExitCode.USAGE,
    )
