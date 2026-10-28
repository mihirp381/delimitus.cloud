"""Pilot social sign-in (decision 002 amendment 2026-10-27). An operator creates a pilot org in
social mode with the providers it may use; its admins invite people by email. A first sign-in
links the provider's subject to the one invited or active person with the verified address;
later sign-ins match the subject only. A directory org never moves to social mode."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.ids import new_id
from ssc_control.audit.chain import Actor, NewEvent, append_event
from ssc_control.identity import connections
from ssc_control.identity.join import Person
from ssc_control.identity.rules import SsoProfile, subject_problem
from ssc_control.snapshot.service import mark_dirty

Provider = Literal["google", "microsoft", "github"]
Mode = Literal["directory", "social"]
WORKOS_PROVIDERS: Final[dict[str, str]] = {
    "google": "GoogleOAuth",
    "microsoft": "MicrosoftOAuth",
    "github": "GitHubOAuth",
}
LABELS: Final[dict[str, str]] = {"google": "Google", "microsoft": "Microsoft", "github": "GitHub"}
FLAG_TO_LINK: Final = frozenset({"github"})
ACTOR: Final = "workos:social"
SocialRefusal = Literal[
    "connection_type_refused",
    "provider_not_enabled",
    "email_not_verified",
    "no_subject",
    "bad_subject",
    "no_email",
    "no_match",
    "ambiguous_email",
    "subject_mismatch",
    "email_domain_not_allowed",
]
MAX_DOMAINS: Final = 10
MAX_DOMAIN: Final = 253
CONSUMER_DOMAINS: Final = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
    }
)
_LABEL: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


@dataclass(frozen=True, slots=True)
class SocialSignIn:
    org_id: str
    providers: tuple[str, ...]
    invite_domains: tuple[str, ...]


type SignIn = connections.DirectoryConnection | SocialSignIn


class SignInError(ValueError):
    pass


def issuer(provider: str) -> str:
    return f"{connections.ISSUER_PREFIX}social:{provider}"


def provider_of(connection_type: str) -> str | None:
    return next((p for p, t in WORKOS_PROVIDERS.items() if t == connection_type), None)


def check_domain(value: str) -> str:
    domain = value.strip()
    if domain.isascii():
        domain = domain.lower()
    labels = domain.split(".")
    if (
        len(domain) > MAX_DOMAIN
        or len(labels) < 2  # noqa: PLR2004
        or not all(_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError(
            f"{value.strip()!r} is not a domain such as acme.com: letters, digits, hyphens and "
            "dots, with no @, scheme, port or path"
        )
    return domain


def check_invite_domains(raw: str) -> tuple[str, ...]:
    domains = tuple(dict.fromkeys(check_domain(d) for d in raw.split(",") if d.strip()))
    if not 1 <= len(domains) <= MAX_DOMAINS:
        raise ValueError(f"invite domains are 1 to {MAX_DOMAINS} comma-separated domains")
    return domains


def consumer_problem(domains: Iterable[str], *, allow_consumer: bool) -> str | None:
    consumer = sorted(set(domains) & CONSUMER_DOMAINS)
    if consumer and not allow_consumer:
        return (
            f"{', '.join(consumer)} is a consumer mail domain; pass --allow-consumer-domain only "
            "for a test org"
        )
    return None


def domain_of(email: str) -> str:
    return email.rpartition("@")[2].lower()


def at_domains(email: str, domains: Iterable[str]) -> bool:
    return "@" in email and domain_of(email) in set(domains)


async def invite_domains(conn: AsyncConnection, org_id: str) -> tuple[str, ...]:
    return tuple((await conn.execute(_DOMAINS, {"org": org_id})).scalar_one_or_none() or ())


def check_providers(raw: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(p.strip().lower() for p in raw.split(",") if p.strip()))
    unknown = [p for p in names if p not in WORKOS_PROVIDERS]
    if not names or unknown:
        raise ValueError(f"providers are a comma-separated subset of {', '.join(WORKOS_PROVIDERS)}")
    return names


_MODE = text("select sign_in_mode, social_providers, invite_domains from ssc.org where id = :org")
_LOCK_MODE = text(
    "select sign_in_mode, social_providers, invite_domains from ssc.org where id = :org for update"
)
_DOMAINS = text("select invite_domains from ssc.org where id = :org")
_IN_USE = text(
    "select id, lower(email) from ssc.user_account where org_id = :org "
    "and status in ('invited', 'active') and not (lower(regexp_replace(email, '^.*@', '')) "
    "= any(cast(:domains as text[]))) order by lower(email), id"
)
_EMPTY = text(
    "select not exists (select 1 from ssc.user_account where org_id = :org) "
    "and not exists (select 1 from ssc.directory_connection where org_id = :org)"
)
_SET_MODE = text(
    "update ssc.org set sign_in_mode = :mode, social_providers = cast(:providers as text[]), "
    "invite_domains = cast(:domains as text[]) where id = :org"
)
_BY_LINK = text(
    "select u.id, u.status = 'active' from ssc.identity_link l join ssc.user_account u "
    "on u.org_id = l.org_id and u.id = l.user_id "
    "where l.org_id = :org and l.issuer = :issuer and l.subject = :subject"
)
_BY_EMAIL = text(
    "select u.id, u.status, exists (select 1 from ssc.identity_link l where l.org_id = u.org_id "
    "and l.user_id = u.id and l.source = 'social') from ssc.user_account u "
    "where u.org_id = :org and lower(u.email) = :email limit 2 for update"
)
_LINK = text(
    "insert into ssc.identity_link (id, org_id, user_id, issuer, subject, source) "
    "values (:id, :org, :user, :issuer, :subject, 'social') returning id"
)
_NAME = text(
    "update ssc.user_account set display_name = :name "
    "where org_id = :org and id = :id and display_name = email"
)
_ACTIVATE = text(
    "update ssc.user_account set status = 'active' where org_id = :org and id = :id "
    "and status = 'invited'"
)


async def mode(conn: AsyncConnection, org_id: str) -> Mode | None:
    row = (await conn.execute(_MODE, {"org": org_id})).one_or_none()
    return None if row is None else cast(Mode, row[0])


async def resolve(conn: AsyncConnection, org_id: str) -> SignIn | None:
    """How the org signs in: its providers in social mode, else its active connection."""
    row = (await conn.execute(_MODE, {"org": org_id})).one_or_none()
    if row is None:
        return None
    if row[0] == "social":
        return SocialSignIn(org_id, tuple(row[1]), tuple(row[2]))
    connection = await connections.load(conn, org_id)
    return None if connection is None or connection.frozen else connection


async def find_person(  # noqa: PLR0911, PLR0912  (one answer per check)
    conn: AsyncConnection, target: SocialSignIn, profile: SsoProfile, chosen: object
) -> Person | SocialRefusal:
    provider = provider_of(profile.connection_type)
    if provider is None:
        return "connection_type_refused"
    if provider not in target.providers or provider != chosen:
        return "provider_not_enabled"
    if profile.email_verified is False:
        return "email_not_verified"
    if not profile.idp_id:
        return "no_subject"
    if subject_problem(profile.idp_id) is not None:
        return "bad_subject"
    if "@" not in profile.email:
        return "no_email"
    if not at_domains(profile.email, target.invite_domains):
        return "email_domain_not_allowed"
    org, iss = target.org_id, issuer(provider)
    linked = (
        await conn.execute(_BY_LINK, {"org": org, "issuer": iss, "subject": profile.idp_id})
    ).one_or_none()
    if linked is not None:
        return Person(str(linked[0]), bool(linked[1]))
    if provider in FLAG_TO_LINK and profile.email_verified is not True:
        return "email_not_verified"
    found = (await conn.execute(_BY_EMAIL, {"org": org, "email": profile.email})).all()
    if not found:
        return "no_match"
    if len(found) > 1:
        return "ambiguous_email"
    user_id, status, has_link = str(found[0][0]), str(found[0][1]), bool(found[0][2])
    if has_link:
        return "subject_mismatch"
    if status == "deactivated":
        return Person(user_id, active=False)
    await _link(conn, org, user_id, iss, profile.idp_id, invited=status == "invited")
    name = " ".join(p for p in (profile.first_name, profile.last_name) if p)[:200]
    if name:
        await conn.execute(_NAME, {"org": org, "id": user_id, "name": name})
    return Person(user_id, active=True)


async def _link(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection, org_id: str, user_id: str, iss: str, subject: str, *, invited: bool
) -> None:
    actor = Actor(kind=ActorKind.INTEGRATION, id=ACTOR)
    link_id = (
        await conn.execute(
            _LINK,
            {
                "id": new_id("idl"),
                "org": org_id,
                "user": user_id,
                "issuer": iss,
                "subject": subject,
            },
        )
    ).scalar_one()
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.IDENTITY_LINKED,
            actor=actor,
            target_kind="identity_link",
            target_id=str(link_id),
            after={"user_id": user_id, "source": "social", "issuer": iss},
        ),
    )
    if invited:
        await conn.execute(_ACTIVATE, {"org": org_id, "id": user_id})
        await append_event(
            conn,
            NewEvent(
                org_id=org_id,
                action=AuditAction.USER_UPDATED,
                actor=actor,
                target_kind="user_account",
                target_id=user_id,
                before={"status": "invited"},
                after={"status": "active"},
            ),
        )
        await mark_dirty(conn, org_id)


async def set_sign_in(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    org_id: str,
    sign_in_mode: Mode,
    providers: Sequence[str],
    *,
    actor: Actor,
    invite_domains: Sequence[str] = (),
    allow_consumer: bool = False,
) -> bool:
    """Set the org's sign-in mode, audited; False when it was already so. Only an org with no
    people and no connection yet, as ``create-social-org`` opens one, may leave directory mode."""
    social = sign_in_mode == "social"
    if social != bool(providers) or social != bool(invite_domains):
        raise SignInError("social mode needs providers and invite domains; directory mode neither")
    if len(invite_domains) > MAX_DOMAINS:
        raise SignInError(f"at most {MAX_DOMAINS} invite domains")
    if (problem := consumer_problem(invite_domains, allow_consumer=allow_consumer)) is not None:
        raise SignInError(problem)
    row = (await conn.execute(_LOCK_MODE, {"org": org_id})).one_or_none()
    if row is None:
        raise SignInError(f"{org_id} is not an org")
    if row[0] == "directory" and social and not await _empty(conn, org_id):
        raise SignInError(
            f"{org_id} is a directory org and cannot move to social mode; a social pilot is a "
            "new org, made with create-social-org"
        )
    if social:
        await _refuse_domains_in_use(conn, org_id, invite_domains)
    before = {
        "sign_in_mode": str(row[0]),
        "social_providers": list(row[1]),
        "invite_domains": list(row[2]),
    }
    after: dict[str, object] = {
        "sign_in_mode": sign_in_mode,
        "social_providers": list(providers),
        "invite_domains": list(invite_domains),
    }
    if before == after:
        return False
    if allow_consumer:
        after["allow_consumer_domain"] = True
    await conn.execute(
        _SET_MODE,
        {
            "org": org_id,
            "mode": sign_in_mode,
            "providers": list(providers),
            "domains": list(invite_domains),
        },
    )
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.ORG_UPDATED,
            actor=actor,
            target_kind="org",
            target_id=org_id,
            before=before,
            after=after,
        ),
    )
    return True


async def _refuse_domains_in_use(
    conn: AsyncConnection, org_id: str, domains: Sequence[str]
) -> None:
    outside = (await conn.execute(_IN_USE, {"org": org_id, "domains": list(domains)})).all()
    if outside:
        people = "; ".join(f"{r[0]} {r[1]}" for r in outside)
        raise SignInError(
            "invited or active people are outside those domains; deactivate them first: " + people
        )


async def _empty(conn: AsyncConnection, org_id: str) -> bool:
    return bool((await conn.execute(_EMPTY, {"org": org_id})).scalar_one())
