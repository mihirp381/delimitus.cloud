"""console.delimitus.com in the control plane (SSC gap 1), run against mocks.

Checks:
  * nothing without ``console_image`` -> test_nothing_is_declared_without_the_image
  * a host rule and path matcher: ``/v1`` to the API, the rest to the console
        -> test_the_console_is_one_more_host_on_the_entry,
        test_only_v1_goes_to_the_api_on_the_console_host
  * a new certificate for that one host, the existing ones unchanged
        -> test_a_third_certificate_names_only_the_console_host
  * its own account with no role -> test_the_service_account_holds_no_role
  * reached only through the load balancer, minimum 0
        -> test_the_service_is_reached_only_through_the_load_balancer
  * the A record in the ``delimitus`` zone -> test_the_a_record_goes_in_the_delimitus_zone
"""

import json
from typing import Any

import pytest

from mockcloud import Declared, entry_address, one, run
from ssc_infra import console, control, naming

PLATFORM_FOLDER = "333333333333"
PUBLIC = "prod"
PROJECT = naming.control_project(PUBLIC)
IMAGE = f"{naming.platform_registry()}/ssc-console@sha256:{'c' * 64}"
LANDING_IMAGE = f"{naming.platform_registry()}/ssc-landing@sha256:{'a' * 64}"
BASE = {
    "platform_folder_id": PLATFORM_FOLDER,
    "control_stages": json.dumps(["staging", PUBLIC]),
    "public_stage": PUBLIC,
}
SERVICE = "gcp:cloudrunv2/service:Service"
BACKEND = "gcp:compute/backendService:BackendService"
URL_MAP = "gcp:compute/uRLMap:URLMap"
CERT = "gcp:compute/managedSslCertificate:ManagedSslCertificate"
PROXY = "gcp:compute/targetHttpsProxy:TargetHttpsProxy"
RECORD = "gcp:dns/recordSet:RecordSet"
EMAIL = naming.sa_email(naming.CONSOLE_SA, PROJECT)


def _run(**config: str) -> list[Declared]:
    return run(naming.PLATFORM_STACK, {**BASE, **config})


@pytest.fixture(scope="module")
def off() -> list[Declared]:
    return _run()


@pytest.fixture(scope="module")
def full() -> list[Declared]:
    return _run(console_image=IMAGE)


@pytest.fixture(scope="module")
def with_landing() -> list[Declared]:
    return _run(console_image=IMAGE, landing="true", landing_image=LANDING_IMAGE)


def _names(declared: list[Declared]) -> set[str]:
    return {d.name for d in declared}


def _of(declared: list[Declared], type_: str) -> dict[str, dict[str, Any]]:
    return {d.name: d.inputs for d in declared if d.type == type_}


def _entry_map(declared: list[Declared]) -> dict[str, Any]:
    (found,) = [
        d.inputs for d in declared if d.type == URL_MAP and d.inputs["name"] == control.ENTRY
    ]
    return found


def _matcher(declared: list[Declared], name: str) -> dict[str, Any]:
    (found,) = [m for m in _entry_map(declared)["pathMatchers"] if m["name"] == name]
    return found


def test_nothing_is_declared_without_the_image(off: list[Declared]) -> None:
    assert not [d for d in off if "console" in d.name]
    assert not [d for d in off if d.inputs.get("name") in {console.SERVICE, control.CONSOLE_CERT}]
    assert naming.CONSOLE_HOST not in json.dumps(_entry_map(off))
    assert not [d for d in off if d.type == RECORD and naming.CONSOLE_HOST in d.inputs["name"]]


def test_config_errors() -> None:
    with pytest.raises(Exception, match="console_image must be"):
        _run(console_image="us-central1-docker.pkg.dev/x/site/ssc-console:latest")
    other = IMAGE.replace(naming.BOOTSTRAP_PROJECT, "elsewhere")
    with pytest.raises(Exception, match="console_image must be"):
        _run(console_image=other)
    with pytest.raises(Exception, match="console_image needs a control stage"):
        run(naming.PLATFORM_STACK, {"platform_folder_id": PLATFORM_FOLDER, "console_image": IMAGE})


def test_only_the_entry_map_and_proxy_change(full: list[Declared], off: list[Declared]) -> None:
    now = {(d.type, d.name): d.inputs for d in full}
    changed = {d.name for d in off if now[(d.type, d.name)] != d.inputs}
    assert changed == {"control-prod-entry-map", "control-prod-entry-https"}
    added = {d.type for d in full if d.name not in _names(off)}
    assert added == {
        "gcp:serviceaccount/account:Account",
        SERVICE,
        "gcp:cloudrunv2/serviceIamMember:ServiceIamMember",
        "gcp:compute/regionNetworkEndpointGroup:RegionNetworkEndpointGroup",
        BACKEND,
        CERT,
        RECORD,
    }


def test_the_console_is_one_more_host_on_the_entry(full: list[Declared]) -> None:
    url_map = _entry_map(full)
    rules = [(r["hosts"], r["pathMatcher"]) for r in url_map["hostRules"]]
    assert rules == [
        (["api.delimitus.com"], "api"),
        (["auth.delimitus.com"], "auth"),
        (["keys.delimitus.com"], "keys"),
        (["console.delimitus.com"], "console"),
    ]
    host_backend = one(full, BACKEND, "control-prod-console-backend")
    api = one(full, BACKEND, "control-prod-api-backend")
    matcher = _matcher(full, control.CONSOLE_MATCHER)
    assert matcher["defaultService"] == f"{host_backend.name}-id"
    assert host_backend.inputs["name"] == console.SERVICE
    assert host_backend.inputs["loadBalancingScheme"] == "EXTERNAL_MANAGED"
    assert host_backend.inputs["logConfig"] == {"enable": False}
    assert matcher["pathRules"] == [{"paths": ["/v1", "/v1/*"], "service": f"{api.name}-id"}]
    assert url_map["defaultService"] == f"{api.name}-id"


def test_only_v1_goes_to_the_api_on_the_console_host(full: list[Declared]) -> None:
    api = one(full, BACKEND, "control-prod-api-backend")
    matcher = _matcher(full, control.CONSOLE_MATCHER)
    to_api = [
        p for r in matcher["pathRules"] if r["service"] == f"{api.name}-id" for p in r["paths"]
    ]
    assert to_api == ["/v1", "/v1/*"]
    every_path = [p for r in matcher["pathRules"] for p in r["paths"]]
    assert not [p for p in every_path if p.startswith("/mcp")]
    assert "routeRules" not in matcher
    assert matcher["defaultService"] != f"{api.name}-id"


def test_the_landing_and_the_console_live_side_by_side(with_landing: list[Declared]) -> None:
    rules = [(r["hosts"], r["pathMatcher"]) for r in _entry_map(with_landing)["hostRules"]]
    assert rules[-3:] == [
        (["delimitus.com"], "landing"),
        (["www.delimitus.com"], "landing"),
        (["console.delimitus.com"], "console"),
    ]
    prod = [v for v in _of(with_landing, CERT).values() if v["project"] == PROJECT]
    assert [v["name"] for v in prod] == [control.ENTRY, control.LANDING_CERT, control.CONSOLE_CERT]


def test_a_third_certificate_names_only_the_console_host(
    full: list[Declared], off: list[Declared], with_landing: list[Declared]
) -> None:
    for declared in (full, with_landing):
        prod = {k: v for k, v in _of(declared, CERT).items() if v["project"] == PROJECT}
        domains = {v["name"]: v["managed"]["domains"] for v in prod.values()}
        assert domains[control.CONSOLE_CERT] == ["console.delimitus.com"]
        assert domains[control.ENTRY] == [
            "api.delimitus.com",
            "auth.delimitus.com",
            "keys.delimitus.com",
        ]
        if control.LANDING_CERT in domains:
            assert domains[control.LANDING_CERT] == ["delimitus.com", "www.delimitus.com"]
        (proxy,) = [v for v in _of(declared, PROXY).values() if v["project"] == PROJECT]
        names = {v["name"]: k for k, v in prod.items()}
        assert proxy["sslCertificates"][-1] == f"{names[control.CONSOLE_CERT]}-id"
        assert proxy["sslCertificates"][0] == f"{names[control.ENTRY]}-id"
    # The existing certificates are the same resources with the same inputs.
    assert {k: v for k, v in _of(off, CERT).items()} == {
        k: v for k, v in _of(full, CERT).items() if v["name"] != control.CONSOLE_CERT
    }


def test_the_service_account_holds_no_role(full: list[Declared]) -> None:
    account = one(full, "gcp:serviceaccount/account:Account", "control-prod-console-sa").inputs
    assert (account["project"], account["accountId"]) == (PROJECT, naming.CONSOLE_SA)
    member = f"serviceAccount:{EMAIL}"
    assert not [d for d in full if d.inputs.get("member") == member]
    assert not [d for d in full if member in d.inputs.get("members", [])]
    mentions = [d.type for d in full if EMAIL in json.dumps(d.inputs)]
    assert mentions == [SERVICE]


def test_the_service_is_reached_only_through_the_load_balancer(full: list[Declared]) -> None:
    service = one(full, SERVICE, f"control-{PUBLIC}-ssc-console").inputs
    assert (service["project"], service["name"]) == (PROJECT, console.SERVICE)
    assert service["ingress"] == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    assert service["scaling"] == {"maxInstanceCount": console.MAX_INSTANCES}
    template = service["template"]
    assert template["serviceAccount"] == EMAIL
    assert template["scaling"] == {"minInstanceCount": 0}
    assert not template.get("volumes")
    [container] = template["containers"]
    assert container["image"] == IMAGE
    assert container["envs"] == [
        {"name": "SSC_CONSOLE_AUTH_ORIGIN", "value": "https://auth.delimitus.com"}
    ]
    neg = one(
        full,
        "gcp:compute/regionNetworkEndpointGroup:RegionNetworkEndpointGroup",
        "control-prod-console-neg",
    ).inputs
    assert neg["networkEndpointType"] == "SERVERLESS"
    assert neg["cloudRun"] == {"service": console.SERVICE}
    invokers = {
        d.inputs["name"]: d.inputs["member"]
        for d in full
        if d.type == "gcp:cloudrunv2/serviceIamMember:ServiceIamMember"
        and d.inputs["project"] == PROJECT
    }
    assert invokers == {"ssc-api": "allUsers", "ssc-auth": "allUsers", "ssc-console": "allUsers"}


def test_the_a_record_goes_in_the_delimitus_zone(full: list[Declared]) -> None:
    records = {d.inputs["name"]: d.inputs for d in full if d.type == RECORD}
    record = records["console.delimitus.com."]
    assert record["project"] == naming.BOOTSTRAP_PROJECT
    assert record["managedZone"] == "delimitus"
    assert (record["type"], record["ttl"]) == ("A", control.DNS_TTL)
    assert record["rrdatas"] == [entry_address(PROJECT)]


def test_only_the_public_stage_hosts_the_console(full: list[Declared]) -> None:
    staging = naming.control_project("staging")
    ours = [
        d
        for d in full
        if d.inputs.get("name") == console.SERVICE or d.inputs.get("accountId") == naming.CONSOLE_SA
    ]
    assert ours
    assert {d.inputs["project"] for d in ours} == {PROJECT}
    assert not [d for d in full if d.name.startswith("control-staging-console")]
    assert staging != PROJECT
