from pydantic import BaseModel, ConfigDict, EmailStr, Field

IDENTITY_HEADER = "X-SSC-Identity"


class IdentityNote(BaseModel):
    """Claims carried in the signed note the gateway attaches to every request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    iss: str
    aud: str
    sub: str = Field(pattern=r"^(usr|sch)_[a-z0-9]{20}$")
    iat: int
    exp: int
    name: str | None = None
    email: EmailStr | None = None
    groups: tuple[str, ...] = ()
