"""delimitus.com's page and form in the control plane (SSC-065, decision 028), run against mocks.

Ticket "done when" checks:
  * hosted on the control entry, no project, load balancer or address of its own
        -> test_it_declares_no_project_address_or_forwarding_rule_of_its_own,
        test_the_apex_and_www_are_two_more_hosts_on_the_entry
  * a second certificate for the apex and ``www``, the control one unchanged
        -> test_a_second_certificate_names_only_the_apex_and_www
  * its account can only create objects in its own bucket
        -> test_the_service_account_can_only_create_requests_in_its_own_bucket
  * built only when ``landing`` is true -> test_nothing_is_declared_until_landing_is_true,
        test_the_first_step_makes_nothing_public
  * records in the ``delimitus`` zone of ``ssc-platform-0`` -> test_the_a_records_...
"""

import json
from typing import Any

import pytest

from mockcloud import Declared, entry_address, one, run
from ssc_infra import control, landing, naming

PLATFORM_FOLDER = "333333333333"
PUBLIC = "prod"
PROJECT = naming.control_project(PUBLIC)
IMAGE = f"{naming.platform_registry()}/ssc-landing@sha256:{'a' * 64}"
BUCKET = f"{PROJECT}-pilot-requests"
BASE = {
    "platform_folder_id": PLATFORM_FOLDER,
    "control_stages": json.dumps(["staging", PUBLIC]),
    "public_stage": PUBLIC,
}
SERVICE = "gcp:cloudrunv2/service:Service"
BUCKET_IAM = "gcp:storage/bucketIAMMember:BucketIAMMember"
URL_MAP = "gcp:compute/uRLMap:URLMap"
CERT = "gcp:compute/managedSslCertificate:ManagedSslCertificate"
PROXY = "gcp:compute/targetHttpsProxy:TargetHttpsProxy"
RECORD = "gcp:dns/recordSet:RecordSet"
EMAIL = naming.sa_email(naming.LANDING_SA, PROJECT)


def _run(**config: str) -> list[Declared]:
    return run(naming.PLATFORM_STACK, {**BASE, **config})


@pytest.fixture(scope="module")
def off() -> list[Declared]:
    return _run()


@pytest.fixture(scope="module")
def first() -> list[Declared]:
    return _run(landing="true")


@pytest.fixture(scope="module")
def full() -> list[Declared]:
    return _run(landing="true", landing_image=IMAGE)


def _names(declared: list[Declared]) -> set[str]:
    return {d.name for d in declared}


def _of(declared: list[Declared], type_: str) -> dict[str, dict[str, Any]]:
    return {d.name: d.inputs for d in declared if d.type == type_}


def _entry_map(declared: list[Declared]) -> dict[str, Any]:
    (found,) = [
        d.inputs for d in declared if d.type == URL_MAP and d.inputs["name"] == control.ENTRY
    ]
    return found


def test_nothing_is_declared_until_landing_is_true(off: list[Declared]) -> None:
    assert not [d for d in off if "landing" in d.name]
    assert not [d for d in off if d.inputs.get("name") == landing.SERVICE]
    hosts = [r["hosts"] for r in _entry_map(off)["hostRules"]]
    assert hosts == [[h] for h in naming.CONTROL_HOSTS]


def test_the_first_step_makes_nothing_public(first: list[Declared], off: list[Declared]) -> None:
    added = [d for d in first if d.name not in _names(off)]
    assert {d.type for d in added} == {
        "gcp:projects/service:Service",
        "gcp:serviceaccount/account:Account",
        "gcp:storage/bucket:Bucket",
        BUCKET_IAM,
        "gcp:monitoring/notificationChannel:NotificationChannel",
        "gcp:monitoring/alertPolicy:AlertPolicy",
    }
    api = one(first, "gcp:projects/service:Service", f"control-{PUBLIC}-landing-monitoring")
    assert api.inputs["service"] == "monitoring.googleapis.com"
    assert _entry_map(first) == _entry_map(off)
    assert _of(first, PROXY) == _of(off, PROXY)
    assert not [d for d in first if d.type in {SERVICE, CERT} and d.name not in _names(off)]


def test_config_errors() -> None:
    with pytest.raises(Exception, match="landing_image needs landing: true"):
        _run(landing_image=IMAGE)
    with pytest.raises(Exception, match="landing_image must be"):
        _run(landing="true", landing_image="us-central1-docker.pkg.dev/x/site/ssc-landing:latest")
    other = IMAGE.replace(naming.BOOTSTRAP_PROJECT, "elsewhere")
    with pytest.raises(Exception, match="landing_image must be"):
        _run(landing="true", landing_image=other)
    with pytest.raises(Exception, match="landing needs a control stage"):
        run(naming.PLATFORM_STACK, {"platform_folder_id": PLATFORM_FOLDER, "landing": "true"})


def test_it_declares_no_project_address_or_forwarding_rule_of_its_own(
    full: list[Declared], off: list[Declared]
) -> None:
    added = [d for d in full if d.name not in _names(off)]
    forbidden = {
        "gcp:organizations/project:Project",
        "gcp:artifactregistry/repository:Repository",
        "gcp:compute/globalAddress:GlobalAddress",
        "gcp:compute/globalForwardingRule:GlobalForwardingRule",
        "gcp:compute/targetHttpsProxy:TargetHttpsProxy",
        "gcp:compute/targetHttpProxy:TargetHttpProxy",
        "gcp:compute/sSLPolicy:SSLPolicy",
    }
    assert not [d.name for d in added if d.type in forbidden]
    assert not [d for d in full if d.inputs.get("projectId") == "ssc-site-0"]
    assert not [d for d in full if "ssc-site-0" in json.dumps(d.inputs)]


def test_only_the_entry_map_and_proxy_change(full: list[Declared], off: list[Declared]) -> None:
    now = {(d.type, d.name): d.inputs for d in full}
    changed = {d.name for d in off if now[(d.type, d.name)] != d.inputs}
    assert changed == {"control-prod-entry-map", "control-prod-entry-https"}


def test_the_service_account_can_only_create_requests_in_its_own_bucket(
    full: list[Declared],
) -> None:
    member = f"serviceAccount:{EMAIL}"
    naming_it = [d for d in full if member in json.dumps(d.inputs)]
    assert [(d.type, d.inputs["role"], d.inputs["bucket"]) for d in naming_it] == [
        (BUCKET_IAM, "roles/storage.objectCreator", BUCKET)
    ]
    mentions = [d.type for d in full if EMAIL in json.dumps(d.inputs)]
    assert sorted(mentions) == sorted([BUCKET_IAM, SERVICE])
    # Nothing at the project, on a secret or on another account.
    for d in full:
        if d.inputs.get("member") == member or member in d.inputs.get("members", []):
            assert d.type == BUCKET_IAM
    assert not [
        d for d in full if d.type.startswith("gcp:secretmanager") and EMAIL in str(d.inputs)
    ]
    others = [d for d in full if d.type == BUCKET_IAM and d.inputs["bucket"] != BUCKET]
    assert not [d for d in others if d.inputs["member"] == member]


def test_the_service_holds_no_secret_and_no_database(full: list[Declared]) -> None:
    service = one(full, SERVICE, f"control-{PUBLIC}-ssc-landing").inputs
    [container] = service["template"]["containers"]
    assert all("valueSource" not in e for e in container["envs"])
    assert not service["template"].get("volumes")
    env = {e["name"]: e["value"] for e in container["envs"]}
    assert env == {
        "SSC_LANDING_BUCKET": BUCKET,
        "SSC_LANDING_ORIGIN": "https://delimitus.com",
        "SSC_LANDING_REDIRECT_HOSTS": "www.delimitus.com",
        "SSC_LANDING_TRUSTED_HOPS": "2",
    }


def test_requests_are_private_and_deleted_after_a_year(full: list[Declared]) -> None:
    bucket = one(full, "gcp:storage/bucket:Bucket", f"control-{PUBLIC}-landing-requests").inputs
    assert bucket["name"] == BUCKET
    assert bucket["project"] == PROJECT
    assert bucket["publicAccessPrevention"] == "enforced"
    assert bucket["uniformBucketLevelAccess"] is True
    assert bucket["versioning"] == {"enabled": False}
    assert bucket["softDeletePolicy"] == {"retentionDurationSeconds": 0}
    assert bucket["lifecycleRules"] == [{"action": {"type": "Delete"}, "condition": {"age": 365}}]


def test_the_service_is_reached_only_through_the_load_balancer(full: list[Declared]) -> None:
    service = one(full, SERVICE, f"control-{PUBLIC}-ssc-landing").inputs
    assert (service["project"], service["name"]) == (PROJECT, landing.SERVICE)
    assert service["ingress"] == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    assert "invokerIamDisabled" not in service
    assert service["scaling"] == {"maxInstanceCount": landing.MAX_INSTANCES}
    template = service["template"]
    assert template["serviceAccount"] == EMAIL
    assert template["scaling"] == {"minInstanceCount": 0}
    assert template["containers"][0]["image"] == IMAGE
    invokers = {
        d.inputs["name"]: d.inputs["member"]
        for d in full
        if d.type == "gcp:cloudrunv2/serviceIamMember:ServiceIamMember"
        and d.inputs["project"] == PROJECT
    }
    assert invokers == {"ssc-api": "allUsers", "ssc-auth": "allUsers", "ssc-landing": "allUsers"}


def test_the_apex_and_www_are_two_more_hosts_on_the_entry(
    full: list[Declared], off: list[Declared]
) -> None:
    url_map = _entry_map(full)
    rules = [(r["hosts"], r["pathMatcher"]) for r in url_map["hostRules"]]
    assert rules == [
        (["api.delimitus.com"], "api"),
        (["auth.delimitus.com"], "auth"),
        (["keys.delimitus.com"], "keys"),
        (["delimitus.com"], "landing"),
        (["www.delimitus.com"], "landing"),
    ]
    matchers = {m["name"]: m["defaultService"] for m in url_map["pathMatchers"]}
    backend = one(full, "gcp:compute/backendService:BackendService", "control-prod-landing-backend")
    assert matchers["landing"] == f"{backend.name}-id"
    assert backend.inputs["name"] == landing.SERVICE
    assert backend.inputs["logConfig"] == {"enable": False}
    assert url_map["defaultService"] == matchers["api"]
    rule = "gcp:compute/globalForwardingRule:GlobalForwardingRule"
    assert _of(full, rule) == _of(off, rule)


def test_a_second_certificate_names_only_the_apex_and_www(full: list[Declared]) -> None:
    certs = _of(full, CERT)
    prod = {k: v for k, v in certs.items() if v["project"] == PROJECT}
    assert {v["name"]: v["managed"]["domains"] for v in prod.values()} == {
        control.ENTRY: ["api.delimitus.com", "auth.delimitus.com", "keys.delimitus.com"],
        control.LANDING_CERT: ["delimitus.com", "www.delimitus.com"],
    }
    (proxy,) = [v for v in _of(full, PROXY).values() if v["project"] == PROJECT]
    names = {v["name"]: k for k, v in prod.items()}
    assert proxy["sslCertificates"] == [
        f"{names[control.ENTRY]}-id",
        f"{names[control.LANDING_CERT]}-id",
    ]


def test_the_a_records_go_in_the_delimitus_zone_of_the_platform_project(
    full: list[Declared],
) -> None:
    records = {d.inputs["name"]: d.inputs for d in full if d.type == RECORD}
    assert {"api.delimitus.com.", "auth.delimitus.com.", "keys.delimitus.com."} <= set(records)
    assert {"delimitus.com.", "www.delimitus.com."} <= set(records)
    for name in ("delimitus.com.", "www.delimitus.com."):
        record = records[name]
        assert record["project"] == naming.BOOTSTRAP_PROJECT
        assert record["managedZone"] == "delimitus"
        assert (record["type"], record["ttl"]) == ("A", control.DNS_TTL)
        assert record["rrdatas"] == [entry_address(PROJECT)]


def test_the_founder_is_told_without_personal_data(full: list[Declared]) -> None:
    channels = {
        d.name: d.inputs
        for d in full
        if d.type == "gcp:monitoring/notificationChannel:NotificationChannel"
        and "landing" in d.name
    }
    (channel,) = channels.values()
    assert channel["type"] == "email"
    assert channel["labels"] == {"email_address": naming.OPERATOR.removeprefix("user:")}
    alerts = {
        d.name: d.inputs
        for d in full
        if d.type == "gcp:monitoring/alertPolicy:AlertPolicy" and "landing" in d.name
    }
    assert set(alerts) == {
        "control-prod-landing-request-stored",
        "control-prod-landing-request-failed",
    }
    stored = alerts["control-prod-landing-request-stored"]
    log_filter = stored["conditions"][0]["conditionMatchedLog"]["filter"]
    assert 'jsonPayload.event="pilot_request_stored"' in log_filter
    assert 'resource.labels.service_name="ssc-landing"' in log_filter
    assert stored["alertStrategy"]["notificationRateLimit"] == {"period": "300s"}


def test_the_notify_address_can_be_set() -> None:
    declared = _run(landing="true", landing_notify_email="founder@example.com")
    (channel,) = [
        d.inputs
        for d in declared
        if d.type == "gcp:monitoring/notificationChannel:NotificationChannel"
        and "landing" in d.name
    ]
    assert channel["labels"] == {"email_address": "founder@example.com"}
