"""How the data gateway resolves a customer host (GA-5).

A cell's VPC answers every public name with a sinkhole address: the no-internet floor that keeps
apps off the internet (``infra/README.md``, "DNS"). A response policy belongs to the whole VPC,
so the data gateway, which shares the VPC's resolver, got the sinkhole too: a Snowflake account,
an Airtable base or a database named by its DNS name resolved to 192.0.2.1 and the connect timed
out (``CONNECTION_UNAVAILABLE``, ``cannot connect: TimeoutError``; found on proof cell 2,
2026-10-08). The three first sandbox sources never met it: Cloud SQL was named by address, and
BigQuery and Cloud Storage are on the VPC's Google bypass list.

The egress proxy had the same problem and answers it by resolving each allowed host through
Google's public resolvers, by address and over TCP (``ssc_egress.envoy.PUBLIC_RESOLVERS``). The
gateway does the same here. :func:`install` replaces ``socket.getaddrinfo`` for the process, which
is where asyncio's ``loop.getaddrinfo`` (asyncpg, asyncmy, anyio and so httpx) and the synchronous
drivers (pytds) all end up. A public name goes to the resolvers; the answer's addresses are then
shaped by the original ``getaddrinfo`` with ``AI_NUMERICHOST``, so the port, the socket type and
the protocol are what the caller asked for and the driver still holds the host name for TLS.
Names the VPC must answer stay with it: Google's (``*.googleapis.com`` to Private Google Access,
``*.run.app``, the metadata server), Cloud SQL private names (``*.goog``), literal addresses and
``localhost``. IPv6 lookups stay with the VPC too: the gateway leaves by IPv4 through the cell NAT.

What this does not change: where the gateway may connect. Its firewall rule (``egress-data``)
allows every address, as it did; resolving a name is only how it learns the address. What the
customer's server sees is still the cell NAT's address (SSC-086).

``SSC_DATAGW_RESOLVERS`` (:mod:`ssc_datagw.settings`) names the resolvers; empty turns this off,
for a gateway outside a cell.
"""

import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

import dns.exception
import dns.resolver

PUBLIC_RESOLVERS: Final = ("8.8.8.8", "8.8.4.4")
"""Google Public DNS, the egress proxy's resolvers (``ssc_egress.envoy.PUBLIC_RESOLVERS``)."""
LIFETIME_SECONDS: Final = 5.0
"""How long one lookup may take over both resolvers; a connect waits 10 s in all."""
VPC_SUFFIXES: Final = ("googleapis.com", "run.app", "internal", "pkg.dev", "goog", "localhost")
"""Names the VPC's resolver answers (``infra/ssc_infra/cell.py``, ``GOOGLE_DNS_PASSTHRU``, and
the Cloud SQL private zone ``sql-psa.goog``)."""

Lookup = Callable[[str], Sequence[str]]
"""A name to its IPv4 addresses, or ``socket.gaierror``."""
GetAddrInfo = Callable[..., list[tuple[Any, Any, Any, str, Any]]]


def resolves_in_vpc(host: str) -> bool:
    """Whether ``host`` is left to the VPC's resolver: a literal address, ``localhost``, or a
    name under :data:`VPC_SUFFIXES`."""
    name = host.rstrip(".").lower()
    if not name:
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return True
    return any(name == suffix or name.endswith("." + suffix) for suffix in VPC_SUFFIXES)


def lookup_with(resolver: dns.resolver.Resolver, lifetime: float = LIFETIME_SECONDS) -> Lookup:
    """The :data:`Lookup` of a dnspython resolver: A records over TCP, no search list, and
    dnspython's failures as the ``socket.gaierror`` a caller of ``getaddrinfo`` expects."""

    def lookup(name: str) -> Sequence[str]:
        try:
            answer = resolver.resolve(name, "A", tcp=True, lifetime=lifetime, search=False)
        except dns.resolver.NXDOMAIN, dns.resolver.NoAnswer:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
        except dns.exception.DNSException as exc:
            raise socket.gaierror(
                socket.EAI_AGAIN, f"the resolvers did not answer: {exc}"
            ) from None
        return [rr.address for rr in answer]  # pyright: ignore[reportAttributeAccessIssue]

    return lookup


def public_resolver(nameservers: Sequence[str]) -> dns.resolver.Resolver:
    """A dnspython resolver that asks ``nameservers`` (addresses) and nothing from the host's
    own configuration."""
    for address in nameservers:
        ipaddress.ip_address(address)
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = list(nameservers)
    return resolver


@dataclass(frozen=True, slots=True)
class PublicLookup:
    """``socket.getaddrinfo`` with public names resolved by ``lookup``; ``original`` shapes the
    answers and takes everything else."""

    lookup: Lookup
    original: GetAddrInfo

    def getaddrinfo(  # noqa: PLR0913, PLR0917  (socket.getaddrinfo's own signature)
        self,
        host: Any,
        port: Any,
        family: int = 0,
        type: int = 0,  # noqa: A002
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[Any, Any, Any, str, Any]]:
        if (
            not isinstance(host, str)
            or family not in (0, socket.AF_INET)
            or flags & socket.AI_NUMERICHOST
            or resolves_in_vpc(host)
        ):
            return self.original(host, port, family, type, proto, flags)
        shaped: list[tuple[Any, Any, Any, str, Any]] = []
        for address in self.lookup(host):
            shaped.extend(
                self.original(
                    address, port, socket.AF_INET, type, proto, flags | socket.AI_NUMERICHOST
                )
            )
        return shaped


def install(nameservers: Sequence[str] = PUBLIC_RESOLVERS) -> PublicLookup:
    """Resolve public names through ``nameservers`` for the rest of the process. The
    :class:`PublicLookup` comes back; its ``original`` restores ``socket.getaddrinfo``."""
    public = PublicLookup(
        lookup=lookup_with(public_resolver(nameservers)), original=socket.getaddrinfo
    )
    socket.getaddrinfo = public.getaddrinfo
    return public
