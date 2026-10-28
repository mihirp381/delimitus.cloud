"""The DNS provider an org's parent zone is hosted at, named from its NS hosts."""

from collections.abc import Iterable, Mapping
from typing import Final

from ssc_control.domains.probe import ProbeError, Resolver, observe

OTHER: Final = "other"
_NEEDLES: Final[tuple[tuple[str, str], ...]] = (
    ("cloudflare", "cloudflare"),
    ("awsdns", "route53"),
    ("domaincontrol", "godaddy"),
    ("azure-dns", "azure"),
    ("googledomains", "google"),
    ("google", "google"),
    ("nsone", "ns1"),
)


def provider_of(name_servers: Iterable[str]) -> str:
    hosts = sorted(h.strip().lower() for h in name_servers)
    for needle, provider in _NEEDLES:
        if any(needle in h for h in hosts):
            return provider
    return OTHER


async def parent_provider(resolvers: Mapping[str, Resolver], suffix: str) -> str | None:
    """The provider of the nearest zone above ``suffix`` that answers NS; None when none does
    or a resolver fails."""
    labels = suffix.split(".")[1:]
    while len(labels) >= 2:  # noqa: PLR2004
        try:
            answers = await observe(resolvers, ".".join(labels))
        except ProbeError:
            return None
        found = frozenset[str]().union(*answers.values())
        if found:
            return provider_of(found)
        labels = labels[1:]
    return None


__all__ = ["OTHER", "parent_provider", "provider_of"]
