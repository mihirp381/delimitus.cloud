"""The app's own Google ID token, for calling the cell's data gateway (SSC-051).

    from ssc_app.workload import WorkloadToken

    tokens = WorkloadToken(audience=data_gateway_url)
    headers = {"Authorization": f"Bearer {tokens.get()}"}

The token comes from Cloud Run's metadata server and must be asked for with ``format=full``:
without it Google leaves out ``email`` and ``email_verified``, and the data gateway, which knows
the app environment only by its service account's email, refuses every query. A token is reused
until five minutes before it expires. The gateway's URL and ``ssc_app.data.query`` arrive with
SSC-052; this is the part that must ask for the token correctly.
"""

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Final

METADATA: Final = "http://metadata.google.internal"
IDENTITY_PATH: Final = "/computeMetadata/v1/instance/service-accounts/default/identity"
REFRESH_BEFORE: Final = 300
TIMEOUT_SECONDS: Final = 5.0


class WorkloadTokenError(RuntimeError):
    """The metadata server did not give a usable token."""


def identity_url(audience: str, metadata: str = METADATA) -> str:
    """The metadata server's ID-token URL for ``audience``, always with ``format=full``."""
    query = urllib.parse.urlencode({"audience": audience, "format": "full"})
    return f"{metadata}{IDENTITY_PATH}?{query}"


def _expiry(token: str) -> float:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, ValueError, KeyError, TypeError) as exc:
        raise WorkloadTokenError("the metadata server returned an unreadable ID token") from exc


class WorkloadToken:
    """ID tokens for one ``audience``, cached and safe to share between threads. ``metadata``
    replaces the metadata server's address (tests)."""

    def __init__(
        self,
        *,
        audience: str,
        metadata: str = METADATA,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._url = identity_url(audience, metadata)
        self._clock = clock
        self._lock = threading.Lock()
        self._token: tuple[str, float] | None = None

    def get(self) -> str:
        """A token valid for at least five more minutes."""
        with self._lock:
            if self._token is None or self._token[1] - REFRESH_BEFORE <= self._clock():
                token = self._fetch()
                self._token = (token, _expiry(token))
            return self._token[0]

    def _fetch(self) -> str:
        request = urllib.request.Request(self._url, headers={"Metadata-Flavor": "Google"})  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                return response.read().decode().strip()
        except urllib.error.HTTPError as exc:
            raise WorkloadTokenError(f"metadata server answered HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise WorkloadTokenError(f"metadata server unreachable: {type(exc).__name__}") from None
