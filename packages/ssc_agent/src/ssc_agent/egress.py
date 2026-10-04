"""Egress proxy credentials, made in the cell (SSC-053).

``issue`` makes a credential for one app environment: a random token, written as a new version
of the environment's ``HTTPS_PROXY`` secret, the proxy URL that carries it
(``ssc_contracts.egress.proxy_url``). The answer holds the credential's id, the digest of its
token and the secret version; the token goes nowhere but Secret Manager, which only the
environment's own identity may read. ``info`` says where the proxy is and the cell's fixed
outbound address, both set by the cell stack.
"""

from dataclasses import dataclass

from ssc_agent.secret_manager import SecretCustody, SecretWriter
from ssc_contracts.app_env import HTTPS_PROXY
from ssc_contracts.egress import new_credential, proxy_url, token_digest
from ssc_shared.runtime import secret_id, service_name


class EgressNotConfiguredError(RuntimeError):
    """The cell has no proxy address, so no credential can name it."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Issued:
    credential_id: str
    sha1: str
    version: str


class ProxyCredentials:
    """Credentials in the cell's Secret Manager; ``proxy_address`` None refuses ``issue``."""

    def __init__(
        self,
        custody: SecretCustody,
        writer: SecretWriter,
        *,
        proxy_address: str | None,
        outbound_ip: str | None,
    ) -> None:
        self._custody = custody
        self._writer = writer
        self.proxy_address = proxy_address
        self.outbound_ip = outbound_ip

    async def issue(self, environment_id: str) -> Issued:
        """A new credential as the next ``HTTPS_PROXY`` version; ``ValueError`` for an id that
        is not an environment's."""
        if self.proxy_address is None:
            raise EgressNotConfiguredError("this agent has no proxy address")
        secret = secret_id(service_name(environment_id), HTTPS_PROXY)
        await self._custody.ensure(secret)
        credential_id, token = new_credential()
        url = proxy_url(environment_id, credential_id, token, self.proxy_address)
        version = await self._writer.add_version(secret, url.encode())
        return Issued(credential_id=credential_id, sha1=token_digest(token), version=version)
