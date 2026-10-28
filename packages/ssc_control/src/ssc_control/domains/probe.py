"""Whether a suffix's parent delegates it to the cell's zone, asked of three public resolvers
over DNS-over-HTTPS (JSON)."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, cast

import httpx2

from ssc_shared.redaction import redact

DOH_ENDPOINTS: Final[Mapping[str, str]] = {
    "cloudflare": "https://cloudflare-dns.com/dns-query",
    "google": "https://dns.google/resolve",
    "quad9": "https://dns.quad9.net:5053/dns-query",
}
TIMEOUT_SECONDS: Final = 10.0
NS_TYPE: Final = 2
NOERROR, NXDOMAIN = 0, 3


class ProbeError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(redact(message))


class Resolver(Protocol):
    async def ns(self, name: str) -> frozenset[str]:
        """The NS hosts the resolver answers for ``name``; empty when it has none."""
        ...


def _host(value: str) -> str:
    return value.strip().lower().removesuffix(".")


class DohResolver(Resolver):
    def __init__(self, url: str, *, client: httpx2.AsyncClient | None = None) -> None:
        self._url = url
        self._client = client or httpx2.AsyncClient(timeout=TIMEOUT_SECONDS)

    async def ns(self, name: str) -> frozenset[str]:
        try:
            response = await self._client.get(
                self._url,
                params={"name": name, "type": "NS"},
                headers={"accept": "application/dns-json"},
            )
            body: object = response.json() if response.is_success else None
        except (httpx2.HTTPError, ValueError) as exc:
            raise ProbeError(f"{self._url}: {type(exc).__name__}") from None
        if not isinstance(body, dict):
            raise ProbeError(f"{self._url}: HTTP {response.status_code}")
        answer = cast("dict[str, Any]", body)
        status = answer.get("Status")
        if status == NXDOMAIN:
            return frozenset()
        if status != NOERROR:
            raise ProbeError(f"{self._url}: DNS status {status}")
        records = answer.get("Answer", [])
        if not isinstance(records, list):
            raise ProbeError(f"{self._url}: Answer is not a list")
        hosts: set[str] = set()
        for record in cast("list[object]", records):
            fields = cast("dict[str, Any]", record) if isinstance(record, dict) else {}
            data = fields.get("data")
            if fields.get("type") == NS_TYPE and isinstance(data, str):
                hosts.add(_host(data))
        return frozenset(hosts)

    async def aclose(self) -> None:
        await self._client.aclose()


def doh_resolvers(client: httpx2.AsyncClient | None = None) -> dict[str, Resolver]:
    return {name: DohResolver(url, client=client) for name, url in DOH_ENDPOINTS.items()}


@dataclass(slots=True)
class FakeResolver(Resolver):
    answers: dict[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])

    async def ns(self, name: str) -> frozenset[str]:
        return self.answers.get(name, frozenset())


async def observe(resolvers: Mapping[str, Resolver], name: str) -> dict[str, frozenset[str]]:
    """Every resolver's answer; ``ProbeError`` when any of them fails."""
    names = list(resolvers)
    answers = await asyncio.gather(*(resolvers[n].ns(name) for n in names))
    return dict(zip(names, answers, strict=True))


def _hosts(values: frozenset[str] | tuple[str, ...]) -> frozenset[str]:
    return frozenset(_host(v) for v in values)


def delegation_seen(observed: Mapping[str, frozenset[str]], ours: frozenset[str]) -> bool:
    """Every resolver answers exactly our name servers."""
    want = _hosts(ours)
    return bool(want and observed) and all(_hosts(v) == want for v in observed.values())


def delegation_gone(observed: Mapping[str, frozenset[str]], ours: frozenset[str]) -> bool:
    """Every resolver answered and none names any of our name servers."""
    want = _hosts(ours)
    return bool(observed) and all(not (_hosts(v) & want) for v in observed.values())


__all__ = [
    "DOH_ENDPOINTS",
    "DohResolver",
    "FakeResolver",
    "ProbeError",
    "Resolver",
    "delegation_gone",
    "delegation_seen",
    "doh_resolvers",
    "observe",
]
