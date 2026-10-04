"""The API token: ``SSC_TOKEN`` if set, else the OS keychain (service ``ssc``, one entry per API).

The keychain entry is either a token kept by ``ssc token set`` or the login ``ssc login`` keeps
(SSC-019): a five-minute access token and the refresh token that replaces it. A login is
refreshed a minute before its access token ends, under a lock file, so two ``ssc`` processes
never present the same refresh token (the auth host ends the login when one is reused).

A coding agent's login (``ssc login --agent NAME``, SSC-048) is kept in a second entry, the API
address followed by ``#agent``, so ``ssc`` goes on acting as the person and only ``ssc mcp`` acts
as the agent.

A machine without a keychain backend (headless Linux, CI) still works through ``SSC_TOKEN``;
keychain failures become a clear error, never a traceback.
"""

import os
import sys
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Final, Literal

import httpx2
import keyring
import typer
from keyring.errors import KeyringError
from pydantic import BaseModel, ConfigDict, ValidationError

from ssc_cli.config import APP_DIR_NAME
from ssc_cli.errors import LOGIN_ENDED, NO_KEYCHAIN, NO_TOKEN, CliError, ExitCode, local_error
from ssc_cli.login import AuthClient, Tokens

if sys.platform != "win32":
    import fcntl

SERVICE: Final = "ssc"
ENV_TOKEN: Final = "SSC_TOKEN"  # noqa: S105  (an environment variable name)
REFRESH_MARGIN: Final = 60
LOCK_FILE: Final = "login.lock"
AGENT_ENTRY: Final = "#agent"

type Bearer = str | Callable[[], str]


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["login"] = "login"
    auth_url: str
    org_id: str
    access_token: str
    expires_at: int
    refresh_token: str
    agent: str | None = None

    @classmethod
    def of(  # noqa: PLR0913  (the login's parts)
        cls, auth_url: str, org_id: str, tokens: Tokens, now: float, agent: str | None = None
    ) -> Login:
        return cls(
            auth_url=auth_url,
            org_id=org_id,
            access_token=tokens.access_token,
            expires_at=int(now) + tokens.expires_in,
            refresh_token=tokens.refresh_token,
            agent=agent,
        )

    def fresh(self, now: float) -> bool:
        return now < self.expires_at - REFRESH_MARGIN


def read_token(api_url: str, env: Mapping[str, str] | None = None) -> str:
    found = bearer(api_url, env)
    return found if isinstance(found, str) else found()


def bearer(
    api_url: str,
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx2.BaseTransport | None = None,
) -> Bearer:
    """``SSC_TOKEN``, the kept token, or for a login a callable that refreshes as needed."""
    stored = _stored(api_url, env)
    login = _as_login(stored)
    if login is None:
        return stored
    return _refreshing(api_url, login, transport)


def agent_bearer(api_url: str, *, transport: httpx2.BaseTransport | None = None) -> Bearer | None:
    """The kept agent login's access token, refreshed as needed; None when there is none."""
    login = read_login(api_url, agent=True)
    return None if login is None else _refreshing(api_url, login, transport)


def _refreshing(api_url: str, login: Login, transport: httpx2.BaseTransport | None) -> Bearer:
    current = login

    def access() -> str:
        nonlocal current
        if not current.fresh(time.time()):
            current = refreshed(api_url, agent=login.agent is not None, transport=transport)
        return current.access_token

    return access


def entry(api_url: str, *, agent: bool = False) -> str:
    """The keychain entry of the person's login, or of the agent's."""
    return f"{api_url}{AGENT_ENTRY}" if agent else api_url


def read_login(api_url: str, *, agent: bool = False) -> Login | None:
    try:
        stored = keyring.get_password(SERVICE, entry(api_url, agent=agent))
    except KeyringError:
        return None
    login = None if stored is None else _as_login(stored)
    return login if login is None or (login.agent is not None) == agent else None


def store_login(api_url: str, login: Login) -> None:
    store_token(entry(api_url, agent=login.agent is not None), login.model_dump_json())


def refreshed(
    api_url: str, *, agent: bool = False, transport: httpx2.BaseTransport | None = None
) -> Login:
    """The kept login with a live access token, refreshing it if no other process has."""
    with _lock():
        login = read_login(api_url, agent=agent)
        if login is None:
            raise _login_ended(api_url)
        if login.fresh(time.time()):
            return login
        with AuthClient(login.auth_url, transport=transport) as auth:
            tokens = auth.refresh(login.refresh_token)
        if tokens is None:
            clear_token(api_url, agent=agent)
            raise _login_ended(api_url, login.org_id, login.agent)
        login = Login.of(login.auth_url, login.org_id, tokens, time.time(), login.agent)
        store_login(api_url, login)
        return login


def _as_login(stored: str) -> Login | None:
    if not stored.startswith("{"):
        return None
    try:
        return Login.model_validate_json(stored)
    except ValidationError:
        return None


@contextmanager
def _lock() -> Generator[None]:
    path = Path(typer.get_app_dir(APP_DIR_NAME)) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        if sys.platform != "win32":
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _login_ended(api_url: str, org_id: str | None = None, agent: str | None = None) -> CliError:
    again = f"ssc login --org {org_id}" if org_id else "ssc login --org <org id>"
    if agent is not None:
        again += f" --agent {agent}"
    return local_error(
        LOGIN_ENDED,
        "Your login has ended.",
        f"Sign in again for {api_url}: `{again}`.",
        ExitCode.AUTH,
    )


def _stored(api_url: str, env: Mapping[str, str] | None) -> str:
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
            f"Run `ssc login` for {api_url}, or set the SSC_TOKEN environment variable.",
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


def clear_token(api_url: str, *, agent: bool = False) -> bool:
    """Remove the stored token, or the agent's login. True when one was removed."""
    kept = entry(api_url, agent=agent)
    try:
        if keyring.get_password(SERVICE, kept) is None:
            return False
        keyring.delete_password(SERVICE, kept)
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
