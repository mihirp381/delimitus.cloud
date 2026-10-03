"""Schedule token v1: how a timer run proves itself to the gateway (SSC-041, decision 020).

A timer call enters the cell the way a browser does, through the app's public host, the cell's
load balancer and the gateway, but it has no browser session. The control plane's worker signs
one token per request and sends it in ``SSC-Schedule-Token``. The gateway verifies it against
the control plane's timer keys (``SSC_TIMER_JWKS``), strips it, and mints the app an ordinary
identity note with ``sub`` the schedule and ``role`` ``schedule``. The app never sees this token.

The token is bound to one request: the app's origin (``aud``), the method (``htm``) and the path
with its query (``htu``), exactly as sent. ``jti`` is the run id, or the run id with ``.start``
for the run's start request to the app's ``health_path``; a gateway instance takes each ``jti``
once. It lives at most ``MAX_TTL_SECONDS``, enough for a gateway at zero to start before it reads
it. The header does not start with ``X-SSC-``, because the gateway drops those before its check.

Pure data. Signing lives in ``ssc_control.timers.https`` and verification in
``ssc_edge.schedule_token``.
"""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEDULE_TOKEN_HEADER: Final = "SSC-Schedule-Token"  # noqa: S105  (a header name, not a secret)
SCHEDULE_TOKEN_TYP: Final = "ssc-sched+jwt"  # noqa: S105  (a JWT type, not a secret)
SCHEDULE_TOKEN_ALG: Final = "ES256"  # noqa: S105  (an algorithm name, not a secret)
MAX_TTL_SECONDS: Final = 120
START_SUFFIX: Final = ".start"

_ORIGIN = r"^https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"


class ScheduleClaims(BaseModel):
    """The claim set of one schedule token. Frozen; unknown claims are refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    aud: str = Field(pattern=_ORIGIN)
    sub: str = Field(pattern=r"^sch_[a-z0-9]{20}$")
    org: str = Field(pattern=r"^org_[a-z0-9]{20}$")
    env: str = Field(pattern=r"^env_[a-z0-9]{20}$")
    htm: Literal["GET", "POST"]
    htu: str = Field(pattern=r"^/[^#\s]*$", max_length=512)
    jti: str = Field(pattern=r"^tmr_[a-z0-9]{20}(\.start)?$")
    iat: int = Field(ge=0)
    exp: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> ScheduleClaims:
        if self.exp <= self.iat:
            raise ValueError("exp must be after iat")
        if self.exp - self.iat > MAX_TTL_SECONDS:
            raise ValueError(f"a schedule token lives at most {MAX_TTL_SECONDS} seconds")
        return self
