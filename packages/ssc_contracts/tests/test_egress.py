"""Allowlist patterns, the proxy's regex for them, the catalogue and credentials (SSC-053)."""

import re

import pytest

from ssc_contracts.app_env import secret_name_problem
from ssc_contracts.egress import (
    CATALOGUE,
    CREDENTIAL_ID,
    DIGEST,
    authority_regex,
    catalogue_entry,
    credential_user,
    host_pattern_problem,
    new_credential,
    pattern_matches,
    proxy_url,
    refusal_message,
    token_digest,
)

ENV = "env_" + "a" * 20


@pytest.mark.parametrize(
    "pattern", ["api.stripe.com", "*.stripe.com", "a-b.example.co.uk", "*.atlassian.net", "x.io"]
)
def test_host_names_and_one_label_wildcards_are_entries(pattern: str) -> None:
    assert host_pattern_problem(pattern) is None


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("https://api.stripe.com", "scheme"),
        ("api.stripe.com/v1", "scheme"),
        ("api.stripe.com:443", "port"),
        ("[::1]", "IP address"),
        ("::1", "IP address"),
        ("10.0.0.1", "IP address"),
        ("1.2.3.4", "IP address"),
        ("*.1.2.3", "IP address"),
        ("API.stripe.com", "lower case"),
        ("*", "wildcard"),
        ("*.com", "DNS host name"),
        ("stripe", "DNS host name"),
        ("api.*.stripe.com", "wildcard"),
        ("*.*.stripe.com", "wildcard"),
        ("*stripe.com", "wildcard"),
        ("api.stripe.com.", "DNS host name"),
        ("-api.stripe.com", "DNS host name"),
        ("a" * 64 + ".com", "DNS host name"),
        (("a" * 60 + ".") * 5 + "com", "253"),
        ("storage.googleapis.com", "Cloud Storage"),
        ("my-bucket.storage.googleapis.com", "Cloud Storage"),
        ("*.storage.googleapis.com", "Cloud Storage"),
        ("*.googleapis.com", "Cloud Storage"),
        ("storage.cloud.google.com", "Cloud Storage"),
        ("*.cloud.google.com", "Cloud Storage"),
    ],
)
def test_other_entries_are_refused(pattern: str, reason: str) -> None:
    problem = host_pattern_problem(pattern)
    assert problem is not None
    assert reason in problem


@pytest.mark.parametrize(
    ("pattern", "host", "allowed"),
    [
        ("api.stripe.com", "api.stripe.com", True),
        ("api.stripe.com", "API.Stripe.com", True),
        ("api.stripe.com", "stripe.com", False),
        ("api.stripe.com", "x.api.stripe.com", False),
        ("*.stripe.com", "api.stripe.com", True),
        ("*.stripe.com", "stripe.com", False),
        ("*.stripe.com", "a.b.stripe.com", False),
        ("*.stripe.com", "apistripe.com", False),
        ("*.stripe.com", "api.stripe.com.evil.io", False),
        ("*.stripe.com", ".stripe.com", False),
    ],
)
def test_the_wildcard_is_exactly_one_label_in_python_and_in_re2(
    pattern: str, host: str, allowed: bool
) -> None:
    assert pattern_matches(pattern, host) is allowed
    assert (re.search(authority_regex(pattern), f"{host}:443") is not None) is allowed


def test_the_regex_takes_port_443_only_and_no_ip_address() -> None:
    regex = authority_regex("*.stripe.com")
    assert regex.startswith("(?i)^")
    assert regex.endswith(":443$")
    for authority in ("api.stripe.com:80", "api.stripe.com:4433", "api.stripe.com", "1.2.3.4:443"):
        assert re.search(regex, authority) is None, authority
    assert re.search(authority_regex("api.stripe.com"), "apixstripe.com:443") is None


def test_every_catalogue_entry_is_a_valid_unique_entry_and_risky_ones_say_why() -> None:
    hosts = [e.host for e in CATALOGUE]
    assert len(hosts) == len(set(hosts))
    for entry in CATALOGUE:
        assert host_pattern_problem(entry.host) is None, entry.host
        assert bool(entry.note) is entry.high_risk, entry.host
    risky = {e.host for e in CATALOGUE if e.high_risk}
    assert {"api.openai.com", "api.anthropic.com", "wetransfer.com"} <= risky
    assert catalogue_entry("api.stripe.com") is not None
    assert catalogue_entry("example.com") is None


def test_the_refusal_names_the_host_and_how_to_ask() -> None:
    message = refusal_message("files.example.com:443")
    assert "files.example.com:443" in message
    assert "[egress] hosts" in message
    assert "admin" in message


def test_a_credential_is_random_and_only_its_digest_is_kept() -> None:
    credential_id, token = new_credential()
    again, other = new_credential()
    assert CREDENTIAL_ID.fullmatch(credential_id)
    assert (credential_id, token) != (again, other)
    assert len(token) >= 43  # noqa: PLR2004  (256 bits)
    digest = token_digest(token)
    assert DIGEST.fullmatch(digest)
    assert token not in digest
    assert token_digest("abc") == "qZk+NkcGgWq6PiVxeFDCbJzQ2J0="
    user = credential_user(ENV, credential_id)
    assert proxy_url(ENV, credential_id, token, "10.20.4.10") == (
        f"http://{user}:{token}@10.20.4.10:3128"
    )


def test_the_proxy_variables_are_the_platform_s() -> None:
    for name in ("HTTPS_PROXY", "NODE_USE_ENV_PROXY", "NO_PROXY"):
        assert secret_name_problem(name) == "is set by the platform"


@pytest.mark.parametrize(
    ("pattern", "host", "allowed"),
    [
        ("pypi.org", "files.pypi.org", False),
        ("*.pythonhosted.org", "files.pythonhosted.org", True),
        ("*.pythonhosted.org", "a.files.pythonhosted.org", False),
        ("registry.npmjs.org", "registry.npmjs.org.exfil.example", False),
        ("*.npmjs.org", "registry.npmjs.org.exfil.example", False),
        ("api.github.com", "evil.example.net", False),
        ("*.internal.example", "instance.internal.example", True),
    ],
)
def test_host_vectors_from_delimitus_egress(pattern: str, host: str, allowed: bool) -> None:
    """Delimitus' registry check takes any depth of subdomain (``host.endswith('.' + r)``);
    an SSC entry takes the name itself or exactly one label more."""
    assert pattern_matches(pattern, host) is allowed
    assert (re.search(authority_regex(pattern), f"{host}:443") is not None) is allowed


@pytest.mark.parametrize(
    "address",
    ["1.1.1.1", "169.254.169.254", "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
)
def test_the_raw_addresses_delimitus_attacks_use_are_never_entries(address: str) -> None:
    assert host_pattern_problem(address) is not None
    for pattern in ("*.stripe.com", "api.stripe.com"):
        assert re.search(authority_regex(pattern), f"{address}:443") is None


@pytest.mark.parametrize("pattern", ["www.googleapis.com", "*.google.com", "sheets.googleapis.com"])
def test_google_hosts_that_are_not_storage_stay_allowed(pattern: str) -> None:
    assert host_pattern_problem(pattern) is None
