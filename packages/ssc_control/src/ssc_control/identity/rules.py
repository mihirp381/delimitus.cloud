"""The login and directory rules, as pure functions (SSC-019, decision 024).

Kept from Delimitus ``identity.sign-in-core``:

- A person is keyed by the directory's stable id, never by a name or an address. A directory
  whose ``idp_id`` is shaped like an email, or a key claim named in
  :data:`FORBIDDEN_SUBJECT_CLAIMS`, is refused, not used.
- A renamed person keeps their account; an address handed to someone new is a different person.
- A login with no subject is refused, with no fallback to the email.
- Every refusal looks the same to the person signing in; the reason goes to the log and audit.

Not kept: Delimitus keyed subjects by ``(idp, subject)`` with no tenant. Here every lookup is
inside one org (forced RLS), and a login is accepted only from the WorkOS organisation and SSO
connections recorded for that org.

Google Workspace SAML sends the email as ``idp_id`` (decision 002), so an org whose join rule is
``email`` joins a login to the one active directory person with that address, every time, and
stores nothing: when the address passes to a new person, the old one is deactivated and the
login finds the new one.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from ssc_shared.hosts import parse_app_host

FORBIDDEN_SUBJECT_CLAIMS: Final = frozenset(
    {
        "email",
        "mail",
        "upn",
        "userprincipalname",
        "username",
        "login",
        "primaryemail",
        "preferred_username",
        "name",
        "displayname",
    }
)
"""Claims that name or address a person and are reassigned; never a key (compared lower-case)."""
REFUSED_CONNECTION_TYPES: Final = frozenset({"GoogleOAuth", "MicrosoftOAuth", "MagicLink"})
"""Consumer sign-in, not the company's directory. Any other ``…OAuth`` type is refused too."""
MAX_RETURN_TO: Final = 2048
MAX_DISPLAY: Final = 320

JoinRule = Literal["idp_id", "email"]
LoginRefusal = Literal[
    "wrong_organization",
    "connection_not_allowed",
    "connection_type_refused",
    "no_subject",
    "bad_subject",
    "no_email",
    "no_match",
    "ambiguous_email",
    "not_active",
]
_CONTROL: Final = re.compile(r"[\x00-\x20\x7f\\]")


class ProfileError(ValueError):
    """A WorkOS answer missing a field every profile or directory user has."""


def _text(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def _need(data: Mapping[str, object], key: str) -> str:
    value = _text(data, key)
    if not value:
        raise ProfileError(f"no {key}")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SsoProfile:
    """The WorkOS SSO profile (``/sso/token``). ``idp_id`` may be empty; checks refuse that."""

    idp_id: str
    organization_id: str
    connection_id: str
    connection_type: str
    email: str
    first_name: str
    last_name: str

    @classmethod
    def from_wire(cls, data: Mapping[str, object]) -> SsoProfile:
        return cls(
            idp_id=_text(data, "idp_id"),
            organization_id=_text(data, "organization_id"),
            connection_id=_need(data, "connection_id"),
            connection_type=_need(data, "connection_type"),
            email=_text(data, "email").lower(),
            first_name=_text(data, "first_name"),
            last_name=_text(data, "last_name"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DirectoryPerson:
    """One WorkOS directory user. ``active`` is ``state == "active"``; anything else is not.
    ``guest``: the directory marks the person a guest (``userType``); never an org member."""

    id: str
    idp_id: str
    email: str
    display_name: str
    active: bool
    guest: bool = False

    @classmethod
    def from_wire(cls, data: Mapping[str, object]) -> DirectoryPerson:
        email = _text(data, "email").lower() or _primary_email(data.get("emails"))
        first, last = _text(data, "first_name"), _text(data, "last_name")
        name = " ".join(p for p in (first, last) if p) or email.split("@", 1)[0] or "Unknown"
        return cls(
            id=_need(data, "id"),
            idp_id=_text(data, "idp_id"),
            email=email[:MAX_DISPLAY],
            display_name=name[:200],
            active=_text(data, "state") == "active",
            guest=_is_guest(data),
        )


def _is_guest(data: Mapping[str, object]) -> bool:
    for key in ("custom_attributes", "raw_attributes"):
        attrs = data.get(key)
        if isinstance(attrs, dict):
            kind = attrs.get("userType") or attrs.get("user_type")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(kind, str) and kind.strip().lower() == "guest":
                return True
    return False


def _primary_email(raw: object) -> str:
    if not isinstance(raw, list):
        return ""
    found = ""
    for item in raw:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, dict):
            continue
        value = item.get("value")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(value, str) or not value:
            continue
        if item.get("primary") is True:  # pyright: ignore[reportUnknownMemberType]
            return value.lower()
        found = found or value.lower()
    return found


def subject_problem(idp_id: str) -> str | None:
    """Why ``idp_id`` cannot key a person, or None. An address is never a key."""
    if not idp_id:
        return "no_subject"
    if "@" in idp_id or idp_id.lower() in FORBIDDEN_SUBJECT_CLAIMS:
        return "email_shaped_subject"
    if len(idp_id) > 300 or _CONTROL.search(idp_id):  # noqa: PLR2004
        return "malformed_subject"
    return None


def check_key_claim(claim: str) -> str:
    """``claim`` if it may key a person; :class:`ValueError` for an addressing claim."""
    if claim.strip().lower() in FORBIDDEN_SUBJECT_CLAIMS:
        raise ValueError(f"{claim!r} names or addresses a person and is never a key")
    return claim


def connection_type_refused(connection_type: str) -> bool:
    return connection_type in REFUSED_CONNECTION_TYPES or connection_type.endswith("OAuth")


def check_profile(
    profile: SsoProfile,
    *,
    organization_id: str,
    connection_ids: frozenset[str],
    join_rule: JoinRule,
) -> LoginRefusal | None:
    """Why this login cannot be accepted for the org, before any person is looked up."""
    if profile.organization_id != organization_id:
        return "wrong_organization"
    if connection_type_refused(profile.connection_type):
        return "connection_type_refused"
    if profile.connection_id not in connection_ids:
        return "connection_not_allowed"
    if not profile.idp_id:
        return "no_subject"
    if join_rule == "email" and "@" not in profile.email:
        return "no_email"
    return None


@dataclass(frozen=True, slots=True)
class ReturnTo:
    host: str
    path: str

    @property
    def url(self) -> str:
        return f"https://{self.host}{self.path}"


def parse_return_to(raw: str, *, apps_domain: str, cell_label: str) -> ReturnTo | None:
    """An app host of the cell and a local path, or None. Never an open redirect: https only, no
    user info, no port, no backslash or control character, the host compared whole."""
    if not raw or len(raw) > MAX_RETURN_TO or _CONTROL.search(raw):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or port is not None
        or parts.username is not None
        or parts.password is not None
        or parts.netloc != host
    ):
        return None
    app = parse_app_host(host, apps_domain)
    if app is None or app.cell_label != cell_label:
        return None
    path = parts.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return None
    return ReturnTo(host, f"{path}?{parts.query}" if parts.query else path)
