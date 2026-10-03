"""The real ``ScheduleDispatcher``: HTTPS to the app's public host (SSC-041, decision 020).

A timer call enters the cell as a browser does: ``https://<app host><path>``, the cell's load
balancer, the gateway. There is no other way in. Each request carries a schedule token
(``ssc_contracts.schedule_token``) signed with the worker's timer key, bound to that origin,
method and path as sent on the wire (percent-encoded as the client encodes it), which the
gateway verifies and strips. Redirects are never followed, so a
redirect is the run's answer (``http_error``). The answer's body is read to its end and dropped,
so the run lasts until the app has finished answering.

The key is an EC P-256 private key from ``SSC_TIMER_SIGNING_KEY`` (PEM) with its id in
``SSC_TIMER_KEY_ID``; its public JWKS (:meth:`ScheduleSigner.jwks`, ``python -m
ssc_control.timers.https``) is every cell gateway's ``SSC_TIMER_JWKS``. The key is never logged.
"""

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import httpx2
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from ssc_contracts.schedule_token import (
    MAX_TTL_SECONDS,
    SCHEDULE_TOKEN_ALG,
    SCHEDULE_TOKEN_HEADER,
    SCHEDULE_TOKEN_TYP,
    START_SUFFIX,
)
from ssc_control.timers.dispatch import DispatchResult, ScheduleDispatcher, TimerCall
from ssc_shared.hosts import app_origin, slug_problem

log = logging.getLogger(__name__)

USER_AGENT: Final = "ssc-timers/1"
CONNECT_SECONDS: Final = 10.0
"""The load balancer is always up, so a connection is quick even when the gateway is at zero."""


class ScheduleSigner:
    """The worker's timer key. Every cell gateway trusts :meth:`jwks` (``SSC_TIMER_JWKS``)."""

    def __init__(
        self,
        private_pem: bytes,
        kid: str,
        *,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        try:
            key = serialization.load_pem_private_key(private_pem, password=None)
        except ValueError, TypeError, UnsupportedAlgorithm:
            raise ValueError("the timer key is not an unencrypted PEM private key") from None
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError("the timer key must be an EC P-256 private key")
        if not kid:
            raise ValueError("the timer key needs an id")
        self._key = key
        self.kid = kid
        self._clock = clock

    def jwks(self) -> dict[str, Any]:
        jwk: dict[str, Any] = dict(ECAlgorithm.to_jwk(self._key.public_key(), as_dict=True))
        jwk.update({"kid": self.kid, "alg": SCHEDULE_TOKEN_ALG, "use": "sig"})
        return {"keys": [jwk]}

    def token(  # noqa: PLR0913  (keyword-only, one per claim)
        self, call: TimerCall, *, origin: str, method: str, path: str, jti: str
    ) -> str:
        """One request's token, minted just before it is sent."""
        now = self._clock()
        claims = {
            "aud": origin,
            "sub": call.schedule_id,
            "org": call.org_id,
            "env": call.environment_id,
            "htm": method,
            "htu": path,
            "jti": jti,
            "iat": now,
            "exp": now + MAX_TTL_SECONDS,
        }
        return jwt.encode(
            claims,
            self._key,
            algorithm=SCHEDULE_TOKEN_ALG,
            headers={"kid": self.kid, "typ": SCHEDULE_TOKEN_TYP},
        )


class HttpsScheduleDispatcher(ScheduleDispatcher):
    """``transport`` replaces the network (tests)."""

    def __init__(
        self,
        signer: ScheduleSigner,
        *,
        apps_domain: str,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._signer = signer
        self._apps_domain = apps_domain
        self._client = httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(None, connect=CONNECT_SECONDS),
            follow_redirects=False,
            headers={"user-agent": USER_AGENT, "accept": "*/*"},
        )

    def origin(self, call: TimerCall) -> str | None:
        """The app's public origin, or None for a slug stored before the host rule refused it."""
        if slug_problem(call.slug) is not None:
            return None
        try:
            return app_origin(call.slug, call.environment, call.cell_label, self._apps_domain)
        except ValueError:
            return None

    async def start(self, call: TimerCall) -> DispatchResult:
        return await self._send(call, "GET", call.health_path, call.run_id + START_SUFFIX)

    async def dispatch(self, call: TimerCall) -> DispatchResult:
        return await self._send(call, call.method, call.path, call.run_id)

    async def _send(self, call: TimerCall, method: str, path: str, jti: str) -> DispatchResult:
        origin = self.origin(call)
        if origin is None:
            log.warning("timer call has no app host", extra={"run_id": call.run_id})
            return DispatchResult(error="dispatch_error")
        url = httpx2.URL(origin + path)
        sent = url.raw_path.decode("ascii")
        token = self._signer.token(call, origin=origin, method=method, path=sent, jti=jti)
        try:
            async with self._client.stream(
                method,
                url,
                headers={SCHEDULE_TOKEN_HEADER: token},
                content=b"" if method == "POST" else None,
            ) as answer:
                async for _ in answer.aiter_raw():
                    pass
                return DispatchResult(http_status=answer.status_code)
        except httpx2.HTTPError as exc:
            log.warning(
                "timer call got no answer",
                extra={"run_id": call.run_id, "error": type(exc).__name__},
            )
            return DispatchResult(error="dispatch_error")

    async def aclose(self) -> None:
        await self._client.aclose()


def new_timer_pem() -> bytes:
    """A fresh P-256 key, PKCS#8 PEM."""
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def main(argv: list[str] | None = None) -> int:
    """``new --out key.pem --kid <id>`` writes a fresh key (mode 0600) and prints its JWKS;
    ``jwks --key key.pem --kid <id>`` prints the JWKS of an existing key. The JWKS is public:
    it is every cell's ``timer_jwks``; the PEM goes to the worker's secret only."""
    parser = argparse.ArgumentParser(prog="python -m ssc_control.timers.https")
    parser.add_argument("command", choices=("new", "jwks"))
    parser.add_argument("--key", "--out", dest="key", type=Path, required=True)
    parser.add_argument("--kid", required=True)
    args = parser.parse_args(argv)
    path: Path = args.key
    if args.command == "new":
        if path.exists():
            sys.stderr.write(f"{path} exists; not overwritten\n")
            return 1
        path.touch(mode=0o600)
        path.write_bytes(new_timer_pem())
    signer = ScheduleSigner(path.read_bytes(), args.kid)
    sys.stdout.write(json.dumps(signer.jwks(), separators=(",", ":"), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
