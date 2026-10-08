"""Identity note v1: the claims the gateway signs into ``X-SSC-Identity`` on every request.

This is a public, versioned contract (``docs/contracts/identity-note.md``). It changes by adding
a new version, never by editing v1. Two rules every consumer must keep:

* Key on ``sub``. It is a ``usr_`` or ``sch_`` id that never changes. ``email`` and ``name`` are
  display strings that a directory can change at any time, and a note for a schedule carries
  neither.
* Verify once per request, when it arrives. Do not re-verify in the middle of a long-lived
  WebSocket or event stream: the note expires five minutes after issue, and ending open streams
  is the kill switch's job, not the app's.

Pure data. Signing lives in ``ssc_edge.identity_note`` and verification in ``ssc_app.identity``.
"""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

IDENTITY_HEADER: Final = "X-SSC-Identity"
IDENTITY_TYP: Final = "ssc-id+jwt"
IDENTITY_ALG: Final = "ES256"
MAX_TTL_SECONDS: Final = 300
MAX_GROUPS: Final = 50

Role = Literal["builder", "user", "schedule"]
EnvironmentName = Literal["prod", "preview"]

_ORIGIN = r"^https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:[0-9]{1,5})?$"
_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class IdentityNote(BaseModel):
    """The claim set of one note. Frozen; unknown claims are refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    iss: str = Field(pattern=r"^https://[a-z0-9.-]+/[a-z0-9-]+$")
    aud: str = Field(pattern=_ORIGIN)
    sub: str = Field(pattern=r"^(usr|sch)_[a-z0-9]{20}$")
    iat: int = Field(ge=0)
    exp: int = Field(ge=0)
    org: str = Field(pattern=r"^org_[a-z0-9]{20}$")
    app: str = Field(pattern=r"^app_[a-z0-9]{20}$")
    env: EnvironmentName
    role: Role
    groups: tuple[str, ...] = Field(default=(), max_length=MAX_GROUPS)
    name: str | None = Field(default=None, min_length=1, max_length=256)
    email: str | None = Field(default=None, pattern=_EMAIL, max_length=320)

    @property
    def is_schedule(self) -> bool:
        return self.sub.startswith("sch_")

    @model_validator(mode="after")
    def _consistent(self) -> IdentityNote:
        if self.exp <= self.iat:
            raise ValueError("exp must be after iat")
        if self.exp - self.iat > MAX_TTL_SECONDS:
            raise ValueError(f"a note lives at most {MAX_TTL_SECONDS} seconds")
        if self.is_schedule:
            if self.role != "schedule":
                raise ValueError("a schedule subject carries role 'schedule'")
            if self.name is not None or self.email is not None:
                raise ValueError("a schedule note carries no name or email")
        elif self.role == "schedule":
            raise ValueError("only a schedule subject carries role 'schedule'")
        for group in self.groups:
            if not group.startswith("grp_") or len(group) != 24:
                raise ValueError(f"not a group id: {group!r}")
        if len(set(self.groups)) != len(self.groups):
            raise ValueError("groups repeat")
        return self
