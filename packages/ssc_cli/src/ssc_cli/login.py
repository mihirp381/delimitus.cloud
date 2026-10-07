"""The auth host's command-line endpoints (SSC-019, decision 024): RFC 8628 device authorisation,
refresh and RFC 7009 revocation. Form posts in, JSON out; an OAuth error is ``{"error": ...}``.

The auth address is ``--auth-url``, then ``SSC_AUTH_URL``, then the API address with its first
label ``api`` swapped for ``auth`` (``https://api.delimitus.com`` -> ``https://auth.delimitus.com``).

Every post is retried, up to three times, only where the auth host did no work: ``429`` after
``Retry-After``, ``502``, ``503`` and ``504`` with backoff, and a connection that never opened. A
refresh token is used once and a second use ends the login, so a ``500`` or a connection lost
mid-request is not retried.
"""

import os
import time
from collections.abc import Mapping
from typing import Final, Literal, cast
from urllib.parse import urlsplit, urlunsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ssc_cli.api import BACKOFF, DEFAULT_TIMEOUT, MAX_RETRY_AFTER, USER_AGENT, Sleep
from ssc_cli.config import normalise_api_url
from ssc_cli.errors import BAD_API_URL, BAD_RESPONSE, NETWORK_ERROR, ExitCode, local_error

ENV_AUTH_URL: Final = "SSC_AUTH_URL"
DEVICE_GRANT: Final = "urn:ietf:params:oauth:grant-type:device_code"
RATE_LIMITED: Final = 429
NOT_REACHED: Final = frozenset({502, 503, 504})

PollError = Literal["authorization_pending", "slow_down", "access_denied", "expired_token"]
_POLL_ERRORS: Final = frozenset(
    {"authorization_pending", "slow_down", "access_denied", "expired_token"}
)


class _Reply(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class DeviceStart(_Reply):
    device_code: str = Field(min_length=1)
    user_code: str = Field(min_length=1)
    verification_uri: str
    verification_uri_complete: str
    expires_in: int = Field(gt=0)
    interval: int = Field(gt=0)


class Tokens(_Reply):
    access_token: str = Field(min_length=1)
    token_type: Literal["Bearer"]
    expires_in: int = Field(gt=0)
    refresh_token: str = Field(min_length=1)


def auth_url_for(api_url: str, override: str | None, env: Mapping[str, str] | None = None) -> str:
    e = os.environ if env is None else env
    raw = override or e.get(ENV_AUTH_URL)
    if raw:
        return normalise_api_url(raw, "auth")
    parts = urlsplit(api_url)
    first, dot, rest = parts.netloc.partition(".")
    if first != "api" or not dot:
        raise local_error(
            BAD_API_URL,
            "The auth address is not known for this API.",
            f"Pass --auth-url or set {ENV_AUTH_URL} for {api_url}.",
            ExitCode.USAGE,
        )
    return urlunsplit((parts.scheme, f"auth.{rest}", "", "", ""))


class AuthClient:
    def __init__(
        self,
        auth_url: str,
        *,
        transport: httpx2.BaseTransport | None = None,
        sleep: Sleep = time.sleep,
    ) -> None:
        self.auth_url = auth_url
        self._sleep = sleep
        self._http = httpx2.Client(
            base_url=auth_url,
            transport=transport,
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def __enter__(self) -> AuthClient:
        return self

    def __exit__(self, *_: object) -> None:
        self._http.close()

    def start(self, org_id: str, agent: str | None = None) -> DeviceStart | None:
        """The device grant, or None when the auth host refuses the org. With ``agent`` the
        login is that coding agent's (SSC-048)."""
        form = {"org": org_id} if agent is None else {"org": org_id, "agent": agent}
        r = self._post("/device/authorize", form)
        if r.status_code == 400:  # noqa: PLR2004
            return None
        return self._parse(r, DeviceStart)

    def poll(self, device_code: str) -> Tokens | PollError:
        r = self._post("/token", {"grant_type": DEVICE_GRANT, "device_code": device_code})
        error = self._error(r)
        if error in _POLL_ERRORS:
            return cast(PollError, error)
        if error is not None:
            return "expired_token"
        return self._parse(r, Tokens)

    def refresh(self, refresh_token: str) -> Tokens | None:
        """New tokens, or None when the login has ended."""
        r = self._post("/token", {"grant_type": "refresh_token", "refresh_token": refresh_token})
        if self._error(r) is not None:
            return None
        return self._parse(r, Tokens)

    def revoke(self, refresh_token: str) -> None:
        r = self._post("/revoke", {"token": refresh_token})
        if r.status_code != 200:  # noqa: PLR2004
            raise self._bad(r)

    def _post(self, path: str, form: Mapping[str, str]) -> httpx2.Response:
        for wait in (*BACKOFF, None):
            try:
                r = self._http.post(path, data=dict(form))
            except (httpx2.ConnectError, httpx2.ConnectTimeout) as e:
                if wait is None:
                    raise self._unreachable(e) from e
                self._sleep(wait)
                continue
            except httpx2.TransportError as e:
                raise self._unreachable(e) from e
            if wait is None or (r.status_code != RATE_LIMITED and r.status_code not in NOT_REACHED):
                return r
            self._sleep(_retry_after(r) if r.status_code == RATE_LIMITED else wait)
        raise AssertionError("unreachable")

    def _unreachable(self, e: Exception) -> Exception:
        return local_error(
            NETWORK_ERROR,
            "The auth host could not be reached.",
            f"{type(e).__name__} talking to {self.auth_url}. "
            "Check the address and your connection, then retry.",
            ExitCode.NETWORK,
        )

    @staticmethod
    def _error(r: httpx2.Response) -> str | None:
        if r.status_code != 400:  # noqa: PLR2004
            return None
        try:
            error = r.json().get("error")
        except ValueError, AttributeError:
            return "invalid_request"
        return error if isinstance(error, str) else "invalid_request"

    def _parse[M: _Reply](self, r: httpx2.Response, model: type[M]) -> M:
        if r.status_code != 200:  # noqa: PLR2004
            raise self._bad(r)
        try:
            return model.model_validate_json(r.content)
        except ValidationError as e:
            raise self._bad(r) from e

    def _bad(self, r: httpx2.Response) -> Exception:
        return local_error(
            BAD_RESPONSE,
            "The auth host sent an answer ssc does not understand.",
            f"HTTP {r.status_code} from {self.auth_url}. Retry, or update ssc.",
        )


def _retry_after(r: httpx2.Response) -> float:
    try:
        seconds = float(r.headers.get("Retry-After", "1"))
    except ValueError:
        seconds = 1.0
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)
