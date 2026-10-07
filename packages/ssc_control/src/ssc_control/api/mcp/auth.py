"""Who may use the agent interface: a user credential that an agent holds.

The SDK asks :meth:`AgentTokenVerifier.verify_token` about every request's bearer. The API's own
:class:`Verifier` checks it on the agent interface's own audience (``SSC_MCP_RESOURCE``, decision
029: what the auth host issues to a remote MCP client, never a ``/v1`` credential nor one from
``ssc login --agent``), and the credential must also carry ``agent: true`` and a ``client_id``.
Anything else is ``None``, which the SDK answers with ``401`` and a ``WWW-Authenticate`` naming
the protected-resource metadata. Refusing every other credential is what makes every call through
this interface recorded as an agent's, without trusting a header. The token's ``resource`` is that
audience, which the SDK checks again (``validate_token_resource``).
"""

import json
import logging
from collections.abc import Mapping

from mcp.server.auth.provider import AccessToken

from ssc_control.api.auth import PrincipalKind, Verifier
from ssc_control.api.problems import Refusal

log = logging.getLogger("ssc.api")


class AgentTokenVerifier:
    def __init__(self, verifier: Verifier, audience: str) -> None:
        self._verifier = verifier
        self._audience = audience

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            principal = self._verifier.verify(token, self._audience)
        except Refusal as e:
            return _refused(e.evidence)
        if principal.kind is not PrincipalKind.USER:
            return _refused({"reason": "not_user", "kind": principal.kind.value})
        if not principal.is_agent:
            return _refused({"reason": "not_agent"})
        if not principal.client_id:
            return _refused({"reason": "no_client_id"})
        # expires_at stays unset: the Verifier has already checked exp, with the API's leeway.
        return AccessToken(
            token=token,
            client_id=principal.client_id,
            scopes=[],
            subject=principal.subject,
            claims={"org": principal.org_id, "jti": principal.credential_id},
            resource=self._audience,
        )


def _refused(evidence: Mapping[str, object]) -> None:
    log.warning(
        "mcp refusal %s",
        json.dumps(
            {"code": "UNAUTHENTICATED", "path": "/mcp", "evidence": dict(evidence)},
            default=str,
            sort_keys=True,
        ),
    )
