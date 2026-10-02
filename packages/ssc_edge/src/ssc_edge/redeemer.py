"""Redeeming the auth host's one-time login code (SSC-019, decision 024).

``POST <auth_url>/internal/redeem`` with ``{org, code, host, nonce}``, authenticated by this
gateway's Google ID token (audience: the auth host's origin), or in dev and test by the rig's
shared secret. The answer names the person; the session id is minted here (``new_sid``).
Anything unexpected is no session: the person is asked to sign in again.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Final, cast

import httpx2

from ssc_edge.session import Session, new_sid
from ssc_edge.tokens import TokenError

log = logging.getLogger(__name__)

REDEEM_PATH: Final = "/internal/redeem"
TIMEOUT_SECONDS: Final = 5.0


def session_of(body: bytes) -> Session | None:
    """The answer as a session with a fresh id, or None when it is not one."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    got = cast(dict[str, object], data)
    sub, org, name, email = (got.get(k) for k in ("sub", "org", "name", "email"))
    iat, exp = got.get("iat"), got.get("exp")
    if not (
        isinstance(sub, str)
        and isinstance(org, str)
        and isinstance(name, str)
        and isinstance(email, str)
        and type(iat) is int
        and type(exp) is int
    ):
        return None
    try:
        return Session(sid=new_sid(), sub=sub, org=org, name=name, email=email, iat=iat, exp=exp)
    except ValueError:
        return None


class HttpRedeemer:
    def __init__(
        self,
        *,
        auth_url: str,
        org_id: str,
        bearer: Callable[[], Awaitable[str]],
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._org = org_id
        self._bearer = bearer
        self._http = httpx2.AsyncClient(
            base_url=auth_url, timeout=TIMEOUT_SECONDS, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def redeem(self, code: str, host: str, nonce: str) -> Session | None:
        try:
            token = await self._bearer()
            r = await self._http.post(
                REDEEM_PATH,
                json={"org": self._org, "code": code, "host": host, "nonce": nonce},
                headers={"authorization": f"Bearer {token}"},
            )
        except (httpx2.HTTPError, TokenError) as e:
            log.warning("login code redemption failed: %s", type(e).__name__)
            return None
        if r.status_code != 200:  # noqa: PLR2004
            log.info("login code refused: HTTP %s", r.status_code)
            return None
        session = session_of(r.content)
        if session is None:
            log.warning("login code redemption answered an invalid session")
        return session
