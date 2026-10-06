"""What the cell registers first, and what its Cloud Run services get at start (SSC-086)."""

from typing import Any

import pytest

from mockcloud import Declared, run
from ssc_infra import cell, naming
from test_cell import (
    ALL,
    A,
    _options,  # pyright: ignore[reportPrivateUsage]
)

SERVICE = "gcp:cloudrunv2/service:Service"
CHAIN = {
    "gcp:certificatemanager/dnsAuthorization:DnsAuthorization::cert-dns-auth",
    "gcp:dns/recordSet:RecordSet::dns-cert-auth",
    "gcp:certificatemanager/certificate:Certificate::cert",
    "gcp:certificatemanager/certificateMap:CertificateMap::cert-map",
    "gcp:certificatemanager/certificateMapEntry:CertificateMapEntry::cert-map-entry",
}


@pytest.fixture(scope="module")
def cell_a() -> list[Declared]:
    return run(naming.cell_stack(A), ALL)


def test_only_the_gateway_boosts_its_cpu_while_starting(cell_a: list[Declared]) -> None:
    resources: dict[str, dict[str, Any]] = {
        d.name: d.inputs["template"]["containers"][0]["resources"]
        for d in cell_a
        if d.type == SERVICE
    }
    assert {"ssc-gateway", "ssc-cell-agent", "ssc-secret-intake", "ssc-datagw"} <= set(resources)
    assert resources["ssc-gateway"]["startupCpuBoost"] is True
    assert {k for k, r in resources.items() if "startupCpuBoost" in r} == {"ssc-gateway"}


def test_the_certificate_is_registered_before_the_network_and_the_sinkhole_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = list(_options(monkeypatch))
    apis = {f"gcp:projects/service:Service::{api.split('.')[0]}" for api in cell.APIS}
    first = {"gcp:organizations/project:Project::project", "pulumi:providers:gcp::gcp"} | apis
    first |= {k for k in order if k.startswith("pulumi:pulumi:StackReference")}
    assert CHAIN <= set(order)
    after_chain = order[max(order.index(k) for k in CHAIN) + 1 :]
    assert set(order[: len(order) - len(after_chain)]) == first | CHAIN
    rules = [k for k in order if k.startswith("gcp:dns/responsePolicyRule:ResponsePolicyRule::")]
    assert rules
    assert max(order.index(k) for k in CHAIN) < min(order.index(k) for k in rules)
    assert max(order.index(k) for k in CHAIN) < order.index("gcp:compute/network:Network::vpc")
