"""Verify the schedule token a timer call carries (SSC-041, ``ssc_contracts.schedule_token``).

The keys are the control plane's public timer JWKS (``SSC_TIMER_JWKS``), at most two during a
rotation. A token verifies when its header is ``ES256`` ``ssc-sched+jwt`` with a known ``kid``,
its signature holds, it names this org, this request's origin, method and path, it is within its
life (30 seconds' leeway either side) and this instance has not taken its ``jti`` before.
"""

import json
from collections.abc import Callable
from typing import Any, Final, cast

import jwt

from ssc_contracts.schedule_token import (
    SCHEDULE_TOKEN_ALG,
    SCHEDULE_TOKEN_TYP,
    ScheduleClaims,
)

LEEWAY_SECONDS: Final = 30
MAX_KEYS: Final = 2


class ScheduleKeysError(ValueError):
    pass


def parse_timer_jwks(raw: str) -> jwt.PyJWKSet:
    """``SSC_TIMER_JWKS``: one or two named public P-256 keys; anything else refuses to start."""
    try:
        doc: object = json.loads(raw)
    except ValueError as exc:
        raise ScheduleKeysError("SSC_TIMER_JWKS is not JSON") from exc
    keys = cast("dict[str, object]", doc).get("keys") if isinstance(doc, dict) else None
    if not isinstance(keys, list) or not 1 <= len(cast("list[object]", keys)) <= MAX_KEYS:
        raise ScheduleKeysError(f"SSC_TIMER_JWKS must hold 1 to {MAX_KEYS} keys")
    for key in cast("list[object]", keys):
        jwk = cast("dict[str, object]", key) if isinstance(key, dict) else {}
        if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256" or not jwk.get("kid") or "d" in jwk:
            raise ScheduleKeysError("SSC_TIMER_JWKS keys must be named public P-256 keys")
    try:
        return jwt.PyJWKSet.from_dict(cast("dict[str, Any]", doc))
    except jwt.PyJWKSetError as exc:
        raise ScheduleKeysError("SSC_TIMER_JWKS does not load") from exc


class ScheduleKeys:
    """One per gateway instance; remembers each ``jti`` it took until the token expires."""

    def __init__(self, keys: jwt.PyJWKSet, *, clock: Callable[[], int]) -> None:
        self._keys = keys
        self._clock = clock
        self._taken: dict[str, int] = {}

    def verify(
        self, token: str, *, origin: str, org: str, method: str, path: str
    ) -> ScheduleClaims | None:
        """The token's claims when it admits this one request, else None."""
        claims = self._signed(token)
        if claims is None:
            return None
        now = self._clock()
        if (
            claims.aud != origin
            or claims.org != org
            or claims.htm != method
            or claims.htu != path
            or now >= claims.exp + LEEWAY_SECONDS
            or claims.iat > now + LEEWAY_SECONDS
        ):
            return None
        self._taken = {k: exp for k, exp in self._taken.items() if exp + LEEWAY_SECONDS > now}
        if claims.jti in self._taken:
            return None
        self._taken[claims.jti] = claims.exp
        return claims

    def _signed(self, token: str) -> ScheduleClaims | None:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("typ") != SCHEDULE_TOKEN_TYP or header.get("alg") != SCHEDULE_TOKEN_ALG:
                return None
            key = self._keys[str(header.get("kid"))]
            raw: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[SCHEDULE_TOKEN_ALG],
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "verify_aud": False,
                    "verify_iss": False,
                    "verify_sub": False,
                    "verify_jti": False,
                },
            )
            return ScheduleClaims.model_validate(raw)
        except KeyError, ValueError, jwt.PyJWTError:
            return None
