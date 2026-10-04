"""The capability diff: a manifest asking for more than the environment grants still deploys."""

from ssc_contracts.capabilities import (
    MAX_LISTED_CHANGES,
    CapabilityDiff,
    EnvironmentCapabilities,
    diff_capabilities,
    render_diff,
)
from ssc_contracts.manifest import Manifest, load_manifest

NONE = EnvironmentCapabilities()


def manifest(**tables: object) -> Manifest:
    return Manifest.model_validate({"schema": "ssc/v1", **tables})


def test_an_ungranted_connection_deploys_but_prints_the_diff() -> None:
    m = load_manifest('schema = "ssc/v1"\n[connections]\nnames = ["warehouse"]\n')
    diff = diff_capabilities(m, NONE)
    assert diff.blocks is False
    assert [(c.severity, c.kind, c.subject, c.approver) for c in diff.changes] == [
        ("high", "connection_missing", "warehouse", "org admin")
    ]
    text = render_diff(diff)
    assert "the deploy continues" in text
    assert "[HIGH] connection_missing warehouse: " in text


def test_changes_are_ordered_high_before_low() -> None:
    m = manifest(
        schedules=[{"name": "nightly", "cron": "0 3 * * *", "path": "/n"}],
        egress={"hosts": ["b.example.com", "a.example.com"]},
        connections={"names": ["warehouse", "crm"]},
        state={"postgres": True},
    )
    diff = diff_capabilities(m, NONE)
    assert [(c.severity, c.kind, c.subject) for c in diff.changes] == [
        ("high", "connection_missing", "crm"),
        ("high", "connection_missing", "warehouse"),
        ("high", "postgres_missing", "postgres"),
        ("medium", "egress_host_missing", "a.example.com"),
        ("medium", "egress_host_missing", "b.example.com"),
        ("low", "schedules_declared", "nightly"),
    ]
    assert all(c.consequence.endswith(".") for c in diff.changes)


def test_the_cap_lists_twenty_and_counts_the_rest() -> None:
    m = manifest(
        connections={"names": [f"c{i:02}" for i in range(20)]},
        schedules=[{"name": f"s{i}", "cron": "0 3 * * *", "path": "/"} for i in range(5)],
    )
    diff = diff_capabilities(m, NONE)
    assert (len(diff.changes), diff.total, diff.summarised) == (MAX_LISTED_CHANGES, 25, True)
    assert {c.severity for c in diff.changes} == {"high"}
    assert render_diff(diff).endswith("... and 5 more changes, not listed.")
    assert diff.model_dump()["summarised"] is True


def test_granted_capabilities_are_not_listed() -> None:
    m = manifest(
        state={"postgres": True},
        connections={"names": ["warehouse"]},
        egress={"hosts": ["api.stripe.com"]},
    )
    caps = EnvironmentCapabilities(
        postgres=True,
        connections=frozenset({"warehouse"}),
        egress_hosts=frozenset({"api.stripe.com"}),
    )
    diff = diff_capabilities(m, caps)
    assert diff == CapabilityDiff(changes=(), total=0)
    assert render_diff(diff).startswith("No change:")


def test_the_default_manifest_asks_for_nothing() -> None:
    assert render_diff(diff_capabilities(manifest(), NONE)).startswith("No change:")


def test_a_listed_pattern_grants_one_label_more() -> None:
    m = manifest(egress={"hosts": ["eu.acme.atlassian.net", "acme.atlassian.net"]})
    caps = EnvironmentCapabilities(egress_hosts=frozenset({"*.atlassian.net"}))
    assert [c.subject for c in diff_capabilities(m, caps).changes] == ["eu.acme.atlassian.net"]
