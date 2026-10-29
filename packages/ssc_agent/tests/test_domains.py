import json
from typing import Any

import httpx2
import pytest

from ssc_agent.__main__ import ConfigError, domains_from_env
from ssc_agent.app import create_app
from ssc_agent.domains import CellDomains, DomainNotFoundError, DomainsError, check_resource
from ssc_contracts.domains import resource_name
from ssc_shared.runtime import ORG_HEADER, ServiceObservation, ServiceSpec

PROJECT = "ssc-c-test"
ORG = "org_" + "a" * 20
SUFFIX = "tools.testorg.com"
ZONE = resource_name(SUFFIX)
ENTRY_IP = "203.0.113.7"
MAP = "ssc-entry"
NS = ["ns-cloud-a1.googledomains.com.", "ns-cloud-a2.googledomains.com."]
DNS = f"/dns/v1/projects/{PROJECT}/managedZones"
CM = f"/v1/projects/{PROJECT}/locations/global"
Json = dict[str, Any]


class Google:
    def __init__(self) -> None:
        self.zones: dict[str, Json] = {}
        self.rrsets: dict[str, dict[tuple[str, str], Json]] = {}
        self.cm: dict[str, Json] = {}
        self.changes: list[Json] = []
        self.calls: list[tuple[str, str]] = []
        self.ops = 0

    def add_zone(self, name: str, dns_name: str) -> None:
        self.zones[name] = {"name": name, "dnsName": dns_name, "nameServers": NS}
        self.rrsets[name] = {
            (dns_name, "NS"): {"name": dns_name, "type": "NS", "ttl": 21600, "rrdatas": NS},
            (dns_name, "SOA"): {"name": dns_name, "type": "SOA", "ttl": 21600, "rrdatas": ["x"]},
        }

    def _op(self) -> Json:
        self.ops += 1
        return {"name": f"projects/{PROJECT}/locations/global/operations/op-{self.ops}"}

    def handler(self, request: httpx2.Request) -> httpx2.Response:  # noqa: PLR0911, PLR0912
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        body: Json = json.loads(request.content) if request.content else {}
        if path.startswith(DNS):
            rest = path.removeprefix(DNS).strip("/").split("/")
            if method == "POST" and rest == [""]:
                if body["name"] in self.zones:
                    return httpx2.Response(409, json={"error": {"message": "exists"}})
                self.add_zone(body["name"], body["dnsName"])
                self.zones[body["name"]] |= {k: v for k, v in body.items() if k != "name"}
                return httpx2.Response(200, json=self.zones[body["name"]])
            zone = self.zones.get(rest[0])
            if zone is None:
                return httpx2.Response(404, json={"error": {"message": "no zone"}})
            if method == "DELETE":
                raise AssertionError("the agent never deletes a zone")
            if len(rest) == 1:
                return httpx2.Response(200, json=zone)
            records = self.rrsets[rest[0]]
            if rest[1] == "rrsets":
                return httpx2.Response(200, json={"rrsets": list(records.values())})
            self.changes.append(body)
            for old in body["deletions"]:
                assert records.pop((old["name"], old["type"])) == old
            for new in body["additions"]:
                assert (new["name"], new["type"]) not in records
                records[(new["name"], new["type"])] = new
            return httpx2.Response(200, json={"status": "done"})
        resource = path.removeprefix("/v1/")
        if "/operations/" in resource:
            return httpx2.Response(200, json={"name": resource, "done": True})
        if method == "POST":
            (param,) = request.url.params.values()
            name = f"{resource}/{param}"
            if name in self.cm:
                return httpx2.Response(409, json={"error": {"message": "exists"}})
            self.cm[name] = {"name": name, **body}
            if resource.endswith("/dnsAuthorizations"):
                self.cm[name]["dnsResourceRecord"] = {
                    "name": f"_acme-challenge.{body['domain']}.",
                    "type": "CNAME",
                    "data": "abc.1.authorize.certificatemanager.goog.",
                }
            if resource.endswith("/certificates"):
                self.cm[name]["managed"]["state"] = "PROVISIONING"
            return httpx2.Response(200, json=self._op() | {"done": False})
        found = self.cm.get(resource)
        if found is None:
            return httpx2.Response(404, json={"error": {"message": "not found"}})
        if method == "DELETE":
            del self.cm[resource]
            return httpx2.Response(200, json=self._op() | {"done": True})
        return httpx2.Response(200, json=found)

    def domains(self) -> CellDomains:
        async def token() -> str:
            return "dummy-token"

        async def no_sleep(_: float) -> None:
            return None

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))
        return CellDomains(
            PROJECT, token, entry_ip=ENTRY_IP, certificate_map=MAP, client=client, sleep=no_sleep
        )

    def records(self) -> dict[tuple[str, str], list[str]]:
        return {k: v["rrdatas"] for k, v in self.rrsets[ZONE].items()}


async def test_create_makes_the_zone_records_certificate_and_map_entry() -> None:
    google = Google()
    made = await google.domains().create(SUFFIX)
    assert made.zone_name == ZONE
    assert made.name_servers == tuple(NS)
    assert made.certificate == f"projects/{PROJECT}/locations/global/certificates/{ZONE}"
    zone = google.zones[ZONE]
    assert (zone["dnsName"], zone["visibility"], zone["description"]) == (
        f"{SUFFIX}.",
        "public",
        SUFFIX,
    )
    assert zone["dnssecConfig"] == {"state": "off"}
    assert google.records() == {
        (f"{SUFFIX}.", "NS"): NS,
        (f"{SUFFIX}.", "SOA"): ["x"],
        (f"*.{SUFFIX}.", "A"): [ENTRY_IP],
        (f"{SUFFIX}.", "CAA"): ['0 issue "pki.goog"'],
        (f"{SUFFIX}.", "MX"): ["0 ."],
        (f"{SUFFIX}.", "TXT"): ['"v=spf1 -all"'],
        (f"_dmarc.{SUFFIX}.", "TXT"): ['"v=DMARC1; p=reject"'],
        (f"_acme-challenge.{SUFFIX}.", "CNAME"): ["abc.1.authorize.certificatemanager.goog."],
    }
    assert len(google.changes) == 2
    auth = google.cm[f"{CM.removeprefix('/v1/')}/dnsAuthorizations/{ZONE}"]
    assert auth["domain"] == SUFFIX
    cert = google.cm[made.certificate]
    assert cert["managed"]["domains"] == sorted([f"*.{SUFFIX}", SUFFIX])
    assert cert["managed"]["dnsAuthorizations"] == [auth["name"]]
    entry = google.cm[
        f"{CM.removeprefix('/v1/')}/certificateMaps/{MAP}/certificateMapEntries/" + ZONE
    ]
    assert (entry["hostname"], entry["certificates"]) == (f"*.{SUFFIX}", [made.certificate])


async def test_create_again_changes_nothing_and_answers_the_same() -> None:
    google = Google()
    domains = google.domains()
    first = await domains.create(SUFFIX)
    changes, resources = len(google.changes), dict(google.cm)
    assert await domains.create(SUFFIX) == first
    assert len(google.changes) == changes
    assert google.cm == resources


async def test_create_replaces_a_record_that_drifted() -> None:
    google = Google()
    domains = google.domains()
    await domains.create(SUFFIX)
    google.rrsets[ZONE][(f"*.{SUFFIX}.", "A")]["rrdatas"] = ["198.51.100.1"]
    await domains.create(SUFFIX)
    assert google.records()[(f"*.{SUFFIX}.", "A")] == [ENTRY_IP]
    assert [len(c["deletions"]) for c in google.changes[-1:]] == [1]


async def test_status_normalises_the_certificate_state() -> None:
    google = Google()
    domains = google.domains()
    with pytest.raises(DomainNotFoundError):
        await domains.status(SUFFIX)
    made = await domains.create(SUFFIX)
    seen = await domains.status(SUFFIX)
    assert (seen.name_servers, seen.state, seen.detail) == (tuple(NS), "provisioning", None)
    managed = google.cm[made.certificate]["managed"]
    managed["state"] = "FAILED"
    managed["provisioningIssue"] = {"reason": "AUTHORIZATION_ISSUE", "details": "no CNAME"}
    managed["authorizationAttemptInfo"] = [
        {"domain": SUFFIX, "state": "FAILED", "failureReason": "CONFIG", "details": "CAA"},
        {"domain": f"*.{SUFFIX}", "state": "AUTHORIZED"},
    ]
    seen = await domains.status(SUFFIX)
    assert seen.state == "failed"
    assert seen.detail == f"AUTHORIZATION_ISSUE no CNAME; {SUFFIX}: CONFIG CAA"
    managed["state"] = "ACTIVE"
    assert (await domains.status(SUFFIX)).state == "active"


async def test_only_ssc_d_resources_are_touched() -> None:
    for name in ("ssc-cell", "ssc-entry", "ssc-d-XYZ", f"{ZONE}0", "sinkhole"):
        with pytest.raises(DomainsError):
            check_resource(name)
    google = Google()
    google.add_zone(ZONE, "other.testorg.com.")
    domains = google.domains()
    with pytest.raises(DomainsError, match="not the zone"):
        await domains.create(SUFFIX)
    with pytest.raises(DomainsError, match="not the zone"):
        await domains.retire(SUFFIX)
    assert google.changes == []
    with pytest.raises(ValueError, match="suffix"):
        await domains.create("testorg.com")


async def test_retire_leaves_the_zone_with_ns_and_soa_only() -> None:
    google = Google()
    domains = google.domains()
    await domains.create(SUFFIX)
    await domains.retire(SUFFIX)
    assert set(google.records()) == {(f"{SUFFIX}.", "NS"), (f"{SUFFIX}.", "SOA")}
    assert ZONE in google.zones
    assert google.cm == {}
    assert not [c for c in google.calls if c[0] == "DELETE" and c[1].startswith(DNS)]
    await domains.retire(SUFFIX)
    assert ZONE in google.zones


class Idle:
    async def apply(self, spec: ServiceSpec) -> str:
        raise AssertionError(spec)

    async def set_traffic(self, service: str, revision: str) -> None:
        raise AssertionError(service)

    async def scale_to_zero(self, service: str) -> None:
        raise AssertionError(service)

    async def remove(self, service: str) -> None:
        raise AssertionError(service)

    async def observe(self, service: str) -> ServiceObservation | None:
        raise AssertionError(service)


def agent(domains: CellDomains | None) -> httpx2.AsyncClient:
    app = create_app(driver=Idle(), org_id=ORG, domains=domains)
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://agent", headers={ORG_HEADER: ORG}
    )


async def test_the_routes_answer_create_status_and_retire() -> None:
    google = Google()
    async with agent(google.domains()) as client:
        made = await client.post("/v1/domains/create", json={"suffix": SUFFIX})
        assert made.status_code == 200
        assert made.json() == {
            "zone_name": ZONE,
            "name_servers": NS,
            "certificate": f"projects/{PROJECT}/locations/global/certificates/{ZONE}",
        }
        seen = await client.post("/v1/domains/status", json={"suffix": SUFFIX})
        assert seen.json() == {"name_servers": NS, "state": "provisioning", "detail": None}
        gone = await client.post("/v1/domains/retire", json={"suffix": SUFFIX})
        assert gone.json() == {"retired": SUFFIX}
        missing = await client.post("/v1/domains/status", json={"suffix": SUFFIX})
        assert (missing.status_code, missing.json()["code"]) == (404, "DOMAIN_NOT_FOUND")
        for bad in ({"suffix": "Tools.testorg.com"}, {"suffix": "testorg.com"}, {}):
            r = await client.post("/v1/domains/create", json=bad)
            assert (r.status_code, r.json()["code"]) == (400, "INVALID_REQUEST")
        r = await client.post("/v1/domains/delete", json={"suffix": SUFFIX})
        assert r.status_code == 404


async def test_without_an_entry_address_domains_are_not_configured() -> None:
    async with agent(None) as client:
        r = await client.post("/v1/domains/create", json={"suffix": SUFFIX})
    assert (r.status_code, r.json()["code"]) == (503, "DOMAINS_NOT_CONFIGURED")


def test_domains_need_both_settings() -> None:
    assert domains_from_env({}) is None
    both = {"SSC_ENTRY_IP": ENTRY_IP, "SSC_CERTIFICATE_MAP": MAP}
    assert domains_from_env(both) == (ENTRY_IP, MAP)
    for half in ({"SSC_ENTRY_IP": ENTRY_IP}, {"SSC_CERTIFICATE_MAP": MAP}):
        with pytest.raises(ConfigError, match="both"):
            domains_from_env(half)
    with pytest.raises(ConfigError, match="IPv4"):
        domains_from_env({**both, "SSC_ENTRY_IP": "entry"})
    with pytest.raises(ConfigError, match="map"):
        domains_from_env({**both, "SSC_CERTIFICATE_MAP": "Bad/Map"})
