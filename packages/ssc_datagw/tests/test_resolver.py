"""The data gateway resolves a customer host through public resolvers, around the cell's DNS
sinkhole (GA-5; :mod:`ssc_datagw.resolver`)."""

import asyncio
import socket
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import dns.exception
import dns.resolver
import pytest
from datagw_world import ENV

from ssc_datagw import resolver
from ssc_datagw.resolver import PUBLIC_RESOLVERS, PublicLookup, install, lookup_with
from ssc_datagw.settings import SettingsError, settings_from_env

SNOWFLAKE = "asigdfr-xh99583.snowflakecomputing.com"
ADDRESSES = ("3.147.132.222", "3.150.20.211")


class Recording:
    """The original ``getaddrinfo`` as the tests see it: every call kept, literal addresses
    shaped as the real one shapes them."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, host: Any, port: Any, *rest: Any) -> list[tuple[Any, Any, Any, str, Any]]:
        self.calls.append((host, port, *rest))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (str(host), port))]


def table_lookup(table: dict[str, Sequence[str]]) -> tuple[resolver.Lookup, list[str]]:
    asked: list[str] = []

    def lookup(name: str) -> Sequence[str]:
        asked.append(name)
        if name not in table:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return table[name]

    return lookup, asked


def test_a_public_name_is_resolved_by_the_lookup_and_shaped_by_the_original() -> None:
    lookup, asked = table_lookup({SNOWFLAKE: ADDRESSES})
    original = Recording()
    public = PublicLookup(lookup=lookup, original=original)

    got = public.getaddrinfo(SNOWFLAKE, 443, socket.AF_INET, socket.SOCK_STREAM, 6, 0)

    assert asked == [SNOWFLAKE]
    assert [entry[4] for entry in got] == [(a, 443) for a in ADDRESSES]
    assert original.calls == [
        (a, 443, socket.AF_INET, socket.SOCK_STREAM, 6, socket.AI_NUMERICHOST) for a in ADDRESSES
    ]


@pytest.mark.parametrize(
    "host",
    [
        "10.21.0.5",
        "34.134.19.137",
        "::1",
        "localhost",
        "metadata.google.internal",
        "storage.googleapis.com",
        "ssc-datagw-abc-uc.a.run.app",
        "us-central1-docker.pkg.dev",
        "x.y.sql-psa.goog",
        "",
    ],
)
def test_the_vpc_keeps_addresses_localhost_and_the_names_it_must_answer(host: str) -> None:
    lookup, asked = table_lookup({})
    original = Recording()
    public = PublicLookup(lookup=lookup, original=original)

    public.getaddrinfo(host, 5432, 0, socket.SOCK_STREAM, 0, 0)

    assert asked == []
    assert original.calls == [(host, 5432, 0, socket.SOCK_STREAM, 0, 0)]


def test_a_numeric_or_ipv6_or_bytes_lookup_goes_to_the_original() -> None:
    lookup, asked = table_lookup({SNOWFLAKE: ADDRESSES})
    original = Recording()
    public = PublicLookup(lookup=lookup, original=original)

    public.getaddrinfo(SNOWFLAKE, 443, socket.AF_INET6, 0, 0, 0)
    public.getaddrinfo(SNOWFLAKE, 443, 0, 0, 0, socket.AI_NUMERICHOST)
    public.getaddrinfo(SNOWFLAKE.encode(), 443)
    public.getaddrinfo(None, 443)

    assert asked == []
    assert len(original.calls) == 4


def test_an_unknown_name_is_the_gaierror_a_driver_expects() -> None:
    lookup, _ = table_lookup({})
    public = PublicLookup(lookup=lookup, original=Recording())
    with pytest.raises(socket.gaierror) as caught:
        public.getaddrinfo("nothing.example", 443)
    assert caught.value.errno == socket.EAI_NONAME


class FakeDns:
    def __init__(self, outcome: Exception | Sequence[str]) -> None:
        self.outcome = outcome
        self.calls: list[tuple[Any, ...]] = []

    def resolve(self, name: str, rdtype: str, **kw: Any) -> list[Any]:
        self.calls.append((name, rdtype, kw))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return [SimpleNamespace(address=a) for a in self.outcome]


def test_the_dnspython_lookup_asks_for_a_records_over_tcp_without_a_search_list() -> None:
    fake = FakeDns(ADDRESSES)
    lookup = lookup_with(fake, lifetime=2.5)  # pyright: ignore[reportArgumentType]
    assert tuple(lookup(SNOWFLAKE)) == ADDRESSES
    assert fake.calls == [(SNOWFLAKE, "A", {"tcp": True, "lifetime": 2.5, "search": False})]


@pytest.mark.parametrize(
    ("failure", "errno"),
    [
        (dns.resolver.NXDOMAIN(), socket.EAI_NONAME),
        (dns.resolver.NoAnswer(), socket.EAI_NONAME),
        (dns.exception.Timeout(), socket.EAI_AGAIN),
        (dns.resolver.NoNameservers(), socket.EAI_AGAIN),
    ],
)
def test_the_dnspython_failures_are_gaierrors(failure: Exception, errno: int) -> None:
    lookup = lookup_with(FakeDns(failure))  # pyright: ignore[reportArgumentType]
    with pytest.raises(socket.gaierror) as caught:
        lookup(SNOWFLAKE)
    assert caught.value.errno == errno
    assert SNOWFLAKE not in str(caught.value) or errno == socket.EAI_AGAIN


def test_the_public_resolver_asks_only_the_named_addresses() -> None:
    made = resolver.public_resolver(PUBLIC_RESOLVERS)
    assert made.nameservers == list(PUBLIC_RESOLVERS)
    with pytest.raises(ValueError, match="does not appear to be an IPv4 or IPv6 address"):
        resolver.public_resolver(("dns.google",))


def test_install_puts_the_lookup_under_asyncio_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncio's ``loop.getaddrinfo`` reads ``socket.getaddrinfo`` at call time, so the drivers
    that connect through the loop resolve through the public lookup after :func:`install`."""
    before = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", before)
    lookup, asked = table_lookup({SNOWFLAKE: ADDRESSES})
    monkeypatch.setattr(
        resolver,
        "lookup_with",
        lambda _resolver, lifetime=0.0: lookup,  # noqa: ARG005
    )

    public = install()

    assert public.original is before
    assert socket.getaddrinfo == public.getaddrinfo

    async def through_the_loop() -> list[Any]:
        return await asyncio.get_running_loop().getaddrinfo(
            SNOWFLAKE, 443, family=socket.AF_INET, type=socket.SOCK_STREAM
        )

    got = asyncio.run(through_the_loop())
    assert asked == [SNOWFLAKE]
    assert sorted(entry[4][0] for entry in got) == sorted(ADDRESSES)


def test_the_resolvers_setting() -> None:
    assert settings_from_env(ENV).resolvers == PUBLIC_RESOLVERS
    assert settings_from_env({**ENV, "SSC_DATAGW_RESOLVERS": ""}).resolvers == ()
    assert settings_from_env({**ENV, "SSC_DATAGW_RESOLVERS": "1.1.1.1, 9.9.9.9"}).resolvers == (
        "1.1.1.1",
        "9.9.9.9",
    )
    with pytest.raises(SettingsError, match="SSC_DATAGW_RESOLVERS"):
        settings_from_env({**ENV, "SSC_DATAGW_RESOLVERS": "dns.google"})
