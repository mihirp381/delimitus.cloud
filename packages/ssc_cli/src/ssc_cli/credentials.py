"""The API token: ``SSC_TOKEN`` if set, else the OS keychain (service ``ssc``, one entry per API).

A machine without a keychain backend (headless Linux, CI) still works through ``SSC_TOKEN``;
keychain failures become a clear error, never a traceback.
"""

import os
from collections.abc import Mapping
from typing import Final

import keyring
from keyring.errors import KeyringError

from ssc_cli.errors import NO_KEYCHAIN, NO_TOKEN, CliError, ExitCode, local_error

SERVICE: Final = "ssc"
ENV_TOKEN: Final = "SSC_TOKEN"  # noqa: S105  (an environment variable name)


def read_token(api_url: str, env: Mapping[str, str] | None = None) -> str:
    e = os.environ if env is None else env
    token = e.get(ENV_TOKEN, "").strip()
    if token:
        return token
    try:
        stored = keyring.get_password(SERVICE, api_url)
    except KeyringError as exc:
        raise local_error(
            NO_TOKEN,
            "No API token.",
            "No keychain is available on this machine. Set the SSC_TOKEN environment variable.",
            ExitCode.AUTH,
        ) from exc
    if not stored:
        raise local_error(
            NO_TOKEN,
            "No API token.",
            f"Run `ssc token set` for {api_url}, or set the SSC_TOKEN environment variable.",
            ExitCode.AUTH,
        )
    return stored


def store_token(api_url: str, token: str) -> None:
    try:
        keyring.set_password(SERVICE, api_url, token)
        kept = keyring.get_password(SERVICE, api_url)
    except KeyringError as exc:
        raise _no_keychain() from exc
    if kept != token:
        raise _no_keychain()


def clear_token(api_url: str) -> bool:
    """Remove the stored token. True when one was removed."""
    try:
        if keyring.get_password(SERVICE, api_url) is None:
            return False
        keyring.delete_password(SERVICE, api_url)
    except KeyringError:
        return False
    return True


def _no_keychain() -> CliError:
    return local_error(
        NO_KEYCHAIN,
        "The token could not be kept in the keychain.",
        "No working keychain is available on this machine. "
        "Set the SSC_TOKEN environment variable instead.",
    )
