"""delimitus.com in the platform program (SSC-065), run against mocks."""

from typing import Any

import pytest

from mockcloud import Declared, one, run
from ssc_infra import landing, naming

PLATFORM_FOLDER = "333333333333"
IMAGE = "us-central1-docker.pkg.dev/ssc-site-0/site/ssc-landing@sha256:" + "a" * 64
DNS_PROJECT = "delimitus-dns-project"


def _run(**config: str) -> list[Declared]:
    return run(naming.PLATFORM_STACK, {"platform_folder_id": PLATFORM_FOLDER, **config})


@pytest.fixture(scope="module")
def full() -> list[Declared]:
    return _run(landing_image=IMAGE, landing_dns_project=DNS_PROJECT)


def _of(declared: list[Declared], type_: str) -> dict[str, dict[str, Any]]:
    return {d.name: d.inputs for d in declared if d.type == type_}


def test_nothing_is_declared_until_config_asks() -> None:
    names = {d.name for d in _run()}
    assert not any(name.startswith("site") for name in names)


def test_the_first_step_makes_no_service_and_no_dns() -> None:
    declared = _run(landing_enabled="true")
    types = {d.type for d in declared if d.name.startswith("site")}
    assert "gcp:storage/bucket:Bucket" in types
    assert "gcp:artifactregistry/repository:Repository" in types
    assert "gcp:cloudrunv2/service:Service" not in types
    assert "gcp:dns/recordSet:RecordSet" not in types


def test_an_image_needs_a_digest_and_the_dns_project() -> None:
    with pytest.raises(Exception, match="digest"):
        _run(landing_image="us-central1-docker.pkg.dev/x/site/ssc-landing:latest")
    with pytest.raises(Exception, match="landing_dns_project"):
        _run(landing_image=IMAGE)


def test_its_own_protected_project_under_the_platform_folder(full: list[Declared]) -> None:
    project = one(full, "gcp:organizations/project:Project", "site").inputs
    assert project["projectId"] == landing.PROJECT
    assert project["folderId"] == PLATFORM_FOLDER
    assert project["deletionPolicy"] == "PREVENT"


def test_the_service_account_can_only_create_requests(full: list[Declared]) -> None:
    email = naming.sa_email(landing.SA, landing.PROJECT)
    member = f"serviceAccount:{email}"
    grants = [
        d
        for d in full
        if "IAM" in d.type or "Iam" in d.type
        if d.inputs.get("member") == member or member in d.inputs.get("members", [])
    ]
    assert [(g.type, g.inputs["role"]) for g in grants] == [
        ("gcp:storage/bucketIAMMember:BucketIAMMember", "roles/storage.objectCreator")
    ]
    assert grants[0].inputs["bucket"] == landing.BUCKET


def test_requests_are_private_and_deleted_after_a_year(full: list[Declared]) -> None:
    bucket = one(full, "gcp:storage/bucket:Bucket", "site-requests").inputs
    assert bucket["name"] == landing.BUCKET
    assert bucket["publicAccessPrevention"] == "enforced"
    assert bucket["uniformBucketLevelAccess"] is True
    assert bucket["versioning"] == {"enabled": False}
    assert bucket["softDeletePolicy"] == {"retentionDurationSeconds": 0}
    assert bucket["lifecycleRules"] == [{"action": {"type": "Delete"}, "condition": {"age": 365}}]


def test_the_service_is_reached_only_through_the_load_balancer(full: list[Declared]) -> None:
    service = one(full, "gcp:cloudrunv2/service:Service").inputs
    assert service["name"] == landing.SERVICE
    assert service["ingress"] == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    assert service["invokerIamDisabled"] is True
    assert service["scaling"] == {"maxInstanceCount": landing.MAX_INSTANCES}
    template = service["template"]
    assert template["serviceAccount"] == naming.sa_email(landing.SA, landing.PROJECT)
    [container] = template["containers"]
    assert container["image"] == IMAGE
    env = {e["name"]: e["value"] for e in container["envs"]}
    assert env == {
        "SSC_LANDING_BUCKET": landing.BUCKET,
        "SSC_LANDING_ORIGIN": "https://delimitus.com",
        "SSC_LANDING_REDIRECT_HOSTS": "www.delimitus.com",
        "SSC_LANDING_TRUSTED_HOPS": "2",
    }
    assert not any("SECRET" in name for name in env)
    assert not [d for d in full if d.type == "gcp:cloudrunv2/serviceIamMember:ServiceIamMember"]


def test_https_for_the_apex_and_www_and_www_moves_to_the_apex(full: list[Declared]) -> None:
    cert = one(full, "gcp:compute/managedSslCertificate:ManagedSslCertificate").inputs
    assert cert["managed"]["domains"] == ["delimitus.com.", "www.delimitus.com."]
    tls = one(full, "gcp:compute/sSLPolicy:SSLPolicy").inputs
    assert tls["minTlsVersion"] == "TLS_1_2"
    maps = _of(full, "gcp:compute/uRLMap:URLMap")
    www = [{"hosts": ["www.delimitus.com"], "pathMatcher": "www"}]
    assert maps["site-urlmap"]["hostRules"] == www
    redirect = maps["site-urlmap"]["pathMatchers"][0]["defaultUrlRedirect"]
    assert redirect["hostRedirect"] == "delimitus.com"
    assert redirect["redirectResponseCode"] == "MOVED_PERMANENTLY_DEFAULT"
    assert maps["site-urlmap-http"]["defaultUrlRedirect"]["httpsRedirect"] is True
    rules = _of(full, "gcp:compute/globalForwardingRule:GlobalForwardingRule")
    assert sorted(r["portRange"] for r in rules.values()) == ["443", "80"]
    assert {r["loadBalancingScheme"] for r in rules.values()} == {"EXTERNAL_MANAGED"}


def test_the_a_records_go_back_in_the_delimitus_zone(full: list[Declared]) -> None:
    records = _of(full, "gcp:dns/recordSet:RecordSet")
    site = {k: v for k, v in records.items() if k.startswith("site-")}
    assert sorted(r["name"] for r in site.values()) == ["delimitus.com.", "www.delimitus.com."]
    for record in site.values():
        assert record["project"] == DNS_PROJECT
        assert record["managedZone"] == "delimitus-com"
        assert record["type"] == "A"
        assert record["rrdatas"] == ["203.0.113.10"]


def test_the_founder_is_told_without_personal_data(full: list[Declared]) -> None:
    channel = one(full, "gcp:monitoring/notificationChannel:NotificationChannel").inputs
    assert channel["type"] == "email"
    assert channel["labels"] == {"email_address": naming.OPERATOR.removeprefix("user:")}
    alerts = _of(full, "gcp:monitoring/alertPolicy:AlertPolicy")
    stored = alerts["site-request-stored"]
    log_filter = stored["conditions"][0]["conditionMatchedLog"]["filter"]
    assert 'jsonPayload.event="pilot_request_stored"' in log_filter
    assert 'resource.labels.service_name="ssc-landing"' in log_filter
    assert stored["alertStrategy"]["notificationRateLimit"] == {"period": "300s"}
    assert "site-request-failed" in alerts
