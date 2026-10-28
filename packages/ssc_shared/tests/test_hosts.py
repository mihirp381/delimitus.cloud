"""The host rule (decision 004, CD-1): round trip, one host per (slug, environment), DNS limits,
and a strict parser over the org's suffixes."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ssc_shared.hosts import (
    LABEL_PATTERN,
    MAX_APPS_DOMAIN,
    MAX_SUFFIX,
    RESERVED_SLUGS,
    SLUG_PATTERN,
    AppHost,
    app_host,
    app_origin,
    cell_project,
    check_apps_domain,
    check_slug,
    check_suffix,
    default_suffix,
    parse_app_host,
    slug_problem,
)

DOMAIN = "apps.test"
LABEL = "k7q2m9xa"
SUFFIX = f"{LABEL}.{DOMAIN}"

slugs = st.from_regex(SLUG_PATTERN, fullmatch=True).filter(lambda s: slug_problem(s) is None)
labels = st.from_regex(LABEL_PATTERN, fullmatch=True)
environments = st.sampled_from(["prod", "preview"])
domains = st.sampled_from([DOMAIN, "delimitusapps.com", "a.b.example"])
suffixes = st.one_of(
    st.builds(default_suffix, labels, domains),
    st.sampled_from(["tools.testorg.com", "x.tools.testorg.com", "apps.a-b.example.co.uk"]),
)


def test_the_form() -> None:
    assert default_suffix(LABEL, DOMAIN) == SUFFIX
    assert app_host("expenses", "prod", SUFFIX) == "expenses.k7q2m9xa.apps.test"
    assert app_host("expenses", "preview", SUFFIX) == "expenses--preview.k7q2m9xa.apps.test"
    assert app_origin("expenses", "prod", SUFFIX) == "https://expenses.k7q2m9xa.apps.test"
    assert (
        app_origin("expenses", "prod", "tools.testorg.com") == "https://expenses.tools.testorg.com"
    )


@given(slugs, environments, suffixes)
def test_round_trip(slug: str, environment: str, suffix: str) -> None:
    host = app_host(slug, environment, suffix)
    assert parse_app_host(host, [suffix]) == AppHost(slug, environment, suffix)


@given(slugs, environments, slugs, environments)
def test_no_two_app_environments_share_a_host(s1: str, e1: str, s2: str, e2: str) -> None:
    same_host = app_host(s1, e1, SUFFIX) == app_host(s2, e2, SUFFIX)
    assert same_host == ((s1, e1) == (s2, e2))


def test_a_preview_host_is_never_another_apps_prod_host() -> None:
    for slug in ("abc", "a-preview", "preview", "x-y"):
        preview = app_host(slug, "preview", SUFFIX)
        parsed = parse_app_host(preview, [SUFFIX])
        assert parsed == AppHost(slug, "preview", SUFFIX)
    with pytest.raises(ValueError, match="--"):
        app_host("a--preview", "prod", SUFFIX)


@given(slugs, environments, labels)
def test_hosts_fit_dns_limits(slug: str, environment: str, label: str) -> None:
    longest = ".".join(["a" * 62] * 2 + ["b" * 60])
    assert len(longest) == MAX_APPS_DOMAIN
    host = app_host(slug, environment, default_suffix(label, longest))
    assert all(1 <= len(part) <= 63 for part in host.split("."))
    assert len(host) <= 253


def test_the_longest_host_is_exactly_the_limit() -> None:
    longest = ".".join(["a" * 62] * 2 + ["b" * 60])
    suffix = default_suffix("a" * 16, longest)
    assert len(suffix) == MAX_SUFFIX
    host = app_host("a" * 40, "preview", suffix)
    assert len(host) == 253
    assert len(host.split(".")[0]) == 49
    with pytest.raises(ValueError, match="apps domain"):
        check_apps_domain("c" + longest)
    with pytest.raises(ValueError, match="apps suffix"):
        check_suffix("c" + suffix)


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "testorg.com",
        "Tools.testorg.com",
        "tools..testorg.com",
        "-a.testorg.com",
        "a.b.c.",
        "a_b.c.d",
    ],
)
def test_bad_suffixes_are_refused(suffix: str) -> None:
    with pytest.raises(ValueError, match="apps suffix"):
        check_suffix(suffix)
    with pytest.raises(ValueError, match="apps suffix"):
        parse_app_host("expenses.tools.testorg.com", [suffix])


def test_the_longest_suffix_a_host_ends_with_wins() -> None:
    org = ["tools.testorg.com", "x.tools.testorg.com"]
    assert parse_app_host("ledger.x.tools.testorg.com", org) == AppHost(
        "ledger", "prod", "x.tools.testorg.com"
    )
    assert parse_app_host("ledger.tools.testorg.com", org) == AppHost(
        "ledger", "prod", "tools.testorg.com"
    )
    assert parse_app_host("a.ledger.tools.testorg.com", org) is None
    assert parse_app_host("x.tools.testorg.com", org) is None


def test_each_suffix_names_its_own_hosts() -> None:
    org = [SUFFIX, "tools.testorg.com", "old.testorg.com"]
    assert parse_app_host(f"ledger--preview.{SUFFIX}", org) == AppHost("ledger", "preview", SUFFIX)
    assert parse_app_host("ledger.old.testorg.com", org) == AppHost(
        "ledger", "prod", "old.testorg.com"
    )
    assert parse_app_host("ledger.tools.otherorg.com", org) is None
    assert parse_app_host("ledger.tools.testorg.com", []) is None
    for reserved in ("api", "ssc--agent", "ssc--secrets", "xn--bcher-kva", "api--preview"):
        assert parse_app_host(f"{reserved}.tools.testorg.com", org) is None


@pytest.mark.parametrize(
    ("slug", "problem"),
    [
        ("a--b", "double_dash"),
        ("xn--bcher-kva", "punycode"),
        ("xn--", "pattern"),
        ("Upper", "pattern"),
        ("9lives", "pattern"),
        ("trailing-", "pattern"),
        ("a" * 41, "pattern"),
        ("xn", "short"),
        ("ab", "short"),
        ("a", "short"),
        ("", "pattern"),
        ("naïve", "pattern"),
        ("a\n", "pattern"),
        *((word, "reserved") for word in sorted(RESERVED_SLUGS)),
    ],
)
def test_refused_slugs(slug: str, problem: str) -> None:
    assert slug_problem(slug) == problem
    with pytest.raises(ValueError, match="slug"):
        check_slug(slug)
    with pytest.raises(ValueError, match="slug"):
        app_host(slug, "prod", SUFFIX)


@given(slugs, environments, labels)
def test_no_label_has_a_double_dash_at_3_4(slug: str, environment: str, label: str) -> None:
    host = app_host(slug, environment, default_suffix(label, DOMAIN))
    assert all(part[2:4] != "--" for part in host.split("."))


def test_xn_and_two_letter_slugs_make_no_host() -> None:
    for slug in ("xn", "ab"):
        with pytest.raises(ValueError, match="at least 3"):
            app_host(slug, "preview", SUFFIX)
        assert parse_app_host(f"{slug}--preview.{SUFFIX}", [SUFFIX]) is None
    assert app_host("abc", "preview", SUFFIX) == f"abc--preview.{SUFFIX}"


def test_reserved_words_are_the_component_reference_list() -> None:
    listed = "www api auth login console admin status static keys ssc mail"
    assert frozenset(listed.split()) == RESERVED_SLUGS
    assert slug_problem("apis") is None
    assert slug_problem("x-ssc") is None


@pytest.mark.parametrize(
    "host",
    [
        "expenses.k7q2m9xa.apps.test.",  # trailing dot
        "Expenses.k7q2m9xa.apps.test",  # not lower-cased
        "expenses.k7q2m9xa.apps.test:443",  # port kept
        "expenses.k7q2m9xa.evilapps.test",  # a longer name ending in the domain
        "expenses.k7q2m9xa.apps.test.evil",
        "k7q2m9xa.apps.test",  # no app
        "a.expenses.k7q2m9xa.apps.test",  # one label too many
        "expenses.K7Q2M9XA.apps.test",
        "expenses.short.apps.test",  # not a cell label
        "expenses.9abcdefgh.apps.test",
        "expenses--staging.k7q2m9xa.apps.test",  # only preview has a suffix
        "expenses--preview--preview.k7q2m9xa.apps.test",
        "--preview.k7q2m9xa.apps.test",
        "api.k7q2m9xa.apps.test",  # reserved
        "api--preview.k7q2m9xa.apps.test",
        "xn--bcher-kva.k7q2m9xa.apps.test",
        "apps.test",
        "",
    ],
)
def test_the_parser_refuses_what_the_rule_never_makes(host: str) -> None:
    assert parse_app_host(host, [SUFFIX]) is None


def test_the_suffix_must_follow_a_dot() -> None:
    suffix = "k7q2m9xa.a.bcdefghij"
    assert parse_app_host("xk7q2m9xa.a.bcdefghij", [suffix]) is None
    assert parse_app_host("xyz.k7q2m9xa.a.bcdefghij", [suffix]) == AppHost("xyz", "prod", suffix)


@given(st.text(alphabet="abcdkmqxz79-.AP:", max_size=40).map(lambda t: t + "." + SUFFIX))
def test_whatever_parses_is_exactly_what_the_rule_makes(host: str) -> None:
    parsed = parse_app_host(host, [SUFFIX, f"q.{SUFFIX}"])
    if parsed is not None:
        assert app_host(parsed.slug, parsed.environment, parsed.suffix) == host


@pytest.mark.parametrize(
    "domain", ["", "test", "Apps.test", "apps..test", "-apps.test", "apps.test.", "a_b.test"]
)
def test_bad_apps_domains_are_refused(domain: str) -> None:
    with pytest.raises(ValueError, match="apps domain"):
        check_apps_domain(domain)
    with pytest.raises(ValueError, match="apps domain"):
        default_suffix(LABEL, domain)


def test_bad_cell_labels_and_environments_are_refused() -> None:
    for label in ("short", "9abcdefgh", "a" * 17, "abcdefg-h"):
        with pytest.raises(ValueError, match="cell label"):
            default_suffix(label, DOMAIN)
    with pytest.raises(ValueError, match="environment"):
        app_host("expenses", "staging", SUFFIX)


@given(labels)
def test_a_cell_project_is_the_prefix_and_the_label(label: str) -> None:
    assert cell_project(label) == f"ssc-c-{label}"
    assert cell_project(LABEL) == "ssc-c-k7q2m9xa"


@pytest.mark.parametrize("label", ["short", "Upper1234", "9abcdefgh", "", "has-dash1"])
def test_a_cell_project_rejects_a_bad_label(label: str) -> None:
    with pytest.raises(ValueError, match="cell label"):
        cell_project(label)
