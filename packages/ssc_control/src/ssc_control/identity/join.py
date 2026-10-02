"""Which person an SSO login is (decision 024). Runs in the org-bound transaction of the login.

In order: a link an org admin made for this connection and subject; then the join rule. ``idp_id``
(Okta): the identity link the directory created under its issuer with the same ``idp_id``.
``email`` (Google Workspace SAML): the one active, directory-linked person with that address;
nothing is stored, so a reassigned address finds whoever holds it now. A login that matches no
one, or more than one person, is kept in ``unlinked_login`` and refused.

An admin link is never made for an address-shaped subject (Google sends the email as the SAML
subject): it would key a person by an address that can be handed to someone else. For those, the
fix is the directory itself.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import AuditAction
from ssc_contracts.ids import new_id
from ssc_control.audit.chain import Actor, NewEvent, append_event
from ssc_control.identity.connections import DirectoryConnection, admin_link_issuer
from ssc_control.identity.rules import LoginRefusal, SsoProfile, check_profile, subject_problem


@dataclass(frozen=True, slots=True)
class Person:
    user_id: str
    active: bool


_BY_LINK = text(
    "select u.id, u.status = 'active' from ssc.identity_link l join ssc.user_account u "
    "on u.org_id = l.org_id and u.id = l.user_id "
    "where l.org_id = :org and l.issuer = :issuer and l.subject = :subject"
)
_BY_EMAIL = text(
    "select u.id from ssc.user_account u where u.org_id = :org and u.status = 'active' "
    "and lower(u.email) = :email and exists (select 1 from ssc.identity_link l "
    "where l.org_id = u.org_id and l.user_id = u.id and l.issuer = :issuer) limit 2"
)
_UNLINKED = text(
    "insert into ssc.unlinked_login (id, org_id, connection_id, subject, email, reason) "
    "values (:id, :org, :conn, :subject, :email, :reason) "
    "on conflict (org_id, connection_id, subject) do update set email = excluded.email, "
    "reason = excluded.reason, attempts = ssc.unlinked_login.attempts + 1, last_seen_at = now() "
    "where ssc.unlinked_login.linked_user_id is null"
)
_PENDING_UNLINKED = text(
    "select connection_id, subject from ssc.unlinked_login "
    "where org_id = :org and id = :id and linked_user_id is null for update"
)
_LINK_UNLINKED = text(
    "update ssc.unlinked_login set linked_user_id = :user, linked_at = now() "
    "where org_id = :org and id = :id"
)
_ACTIVE_USER = text(
    "select 1 from ssc.user_account where org_id = :org and id = :id and status = 'active'"
)
_ADMIN_LINK = text(
    "insert into ssc.identity_link (id, org_id, user_id, issuer, subject, source) "
    "values (:id, :org, :user, :issuer, :subject, 'admin') returning id"
)


async def _by_link(conn: AsyncConnection, org_id: str, issuer: str, subject: str) -> Person | None:
    row = (
        await conn.execute(_BY_LINK, {"org": org_id, "issuer": issuer, "subject": subject})
    ).one_or_none()
    return None if row is None else Person(str(row[0]), bool(row[1]))


async def find_person(
    conn: AsyncConnection, connection: DirectoryConnection, profile: SsoProfile
) -> Person | LoginRefusal:
    """The person, or why the login is refused. Records unmatched logins."""
    refused = check_profile(
        profile,
        organization_id=connection.workos_organization_id,
        connection_ids=connection.sso_connection_ids,
        join_rule=connection.join_rule,
    )
    if refused is not None:
        return refused
    org = connection.org_id
    keyable = subject_problem(profile.idp_id) is None
    if keyable:
        issuer = admin_link_issuer(profile.connection_id)
        linked = await _by_link(conn, org, issuer, profile.idp_id)
        if linked is not None:
            return linked
    if connection.join_rule == "idp_id":
        if not keyable:
            return "bad_subject"
        person = await _by_link(conn, org, connection.issuer, profile.idp_id)
        if person is not None:
            return person
        reason: LoginRefusal = "no_match"
    else:
        found = (
            (
                await conn.execute(
                    _BY_EMAIL, {"org": org, "email": profile.email, "issuer": connection.issuer}
                )
            )
            .scalars()
            .all()
        )
        if len(found) == 1:
            return Person(str(found[0]), active=True)
        reason = "ambiguous_email" if found else "no_match"
    await conn.execute(
        _UNLINKED,
        {
            "id": new_id("ulg"),
            "org": org,
            "conn": profile.connection_id,
            "subject": profile.idp_id[:300],
            "email": profile.email[:320],
            "reason": reason,
        },
    )
    return reason


class LinkError(ValueError):
    """The unlinked login is gone or already linked, its subject is an address, or the person is
    not active."""


async def link_unlinked(
    conn: AsyncConnection, org_id: str, unlinked_id: str, user_id: str, *, actor: Actor
) -> str:
    """An org admin ties an unlinked login to an active person. Stored and audited."""
    row = (await conn.execute(_PENDING_UNLINKED, {"org": org_id, "id": unlinked_id})).one_or_none()
    if row is None:
        raise LinkError("not_found")
    connection_id, subject = row
    if subject_problem(str(subject)) is not None:
        raise LinkError("subject_not_keyable")
    if (await conn.execute(_ACTIVE_USER, {"org": org_id, "id": user_id})).one_or_none() is None:
        raise LinkError("user_not_active")
    await conn.execute(_LINK_UNLINKED, {"org": org_id, "id": unlinked_id, "user": user_id})
    link_id = (
        await conn.execute(
            _ADMIN_LINK,
            {
                "id": new_id("idl"),
                "org": org_id,
                "user": user_id,
                "issuer": admin_link_issuer(str(connection_id)),
                "subject": str(subject),
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
            after={"user_id": user_id, "source": "admin", "unlinked_login_id": unlinked_id},
        ),
    )
    return str(link_id)
