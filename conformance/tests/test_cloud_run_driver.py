"""``CloudRunDriver`` passes the ``RuntimeDriver`` contract against the Cloud Run emulator, both
directly and as the control plane reaches it: ``CellAgentDriver`` → agent app → driver."""

from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

import httpx2
import pytest

from ssc_agent.app import create_app
from ssc_agent.cloud_run import CellRuntime, CloudRunDriver
from ssc_conformance.cloud_run_emulator import PROJECT, REGION, CloudRunEmulator
from ssc_conformance.contracts.runtime_driver import (
    CONTRACT_ORG,
    Images,
    RuntimeDriverContract,
    Settle,
    new_spec,
    observed,
    revision,
    traffic,
)
from ssc_contracts.manifest import ResourceClassName
from ssc_control.runtime.cell_agent import CellAgentDriver
from ssc_control.runtime.driver import RuntimeDriverError, ServiceNotFoundError
from ssc_control.runtime.reconciler import reconcile_once
from ssc_shared.runtime import ORG_HEADER, Billing

FIRST = "sha256:" + "1" * 64
SECOND = "sha256:" + "2" * 64
GATEWAY = f"ssc-gateway@{PROJECT}.iam.gserviceaccount.com"
CELL = CellRuntime(
    project=PROJECT,
    region=REGION,
    network=f"projects/{PROJECT}/global/networks/ssc-cell",
    subnetwork=f"projects/{PROJECT}/regions/{REGION}/subnetworks/apps",
    image_repository=f"{REGION}-docker.pkg.dev/{PROJECT}/ssc-apps/apps",
    invoker=GATEWAY,
)
AGENT = "https://ssc-cell-agent.test"


@pytest.fixture
def emulator() -> CloudRunEmulator:
    return CloudRunEmulator()


@pytest.fixture
async def cloud_run(emulator: CloudRunEmulator) -> AsyncIterator[CloudRunDriver]:
    async def token() -> str:
        return "access-token"

    async def time_passes(_: float) -> None:
        emulator.settle()

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(emulator.handler))
    driver = CloudRunDriver(CELL, token, client=client, sleep=time_passes)
    yield driver
    await driver.aclose()


@pytest.fixture
def images() -> Images:
    return Images(FIRST, SECOND)


@pytest.fixture
def settle(emulator: CloudRunEmulator) -> Settle:
    async def done() -> None:
        emulator.settle()

    return done


class TestCloudRunDriver(RuntimeDriverContract):
    @pytest.fixture
    def runtime_driver(self, cloud_run: CloudRunDriver) -> CloudRunDriver:
        return cloud_run


class TestCellAgentDriver(RuntimeDriverContract):
    @pytest.fixture
    async def runtime_driver(self, cloud_run: CloudRunDriver) -> AsyncIterator[CellAgentDriver]:
        audiences: list[str] = []

        async def id_token(audience: str) -> str:
            audiences.append(audience)
            return "id-token"

        transport = httpx2.ASGITransport(app=create_app(cloud_run, org_id=CONTRACT_ORG))
        driver = CellAgentDriver(
            AGENT, id_token, org_id=CONTRACT_ORG, client=httpx2.AsyncClient(transport=transport)
        )
        yield driver
        await driver.aclose()
        assert set(audiences) <= {AGENT}


# ── what the service looks like in Cloud Run ─────────────────────────────────


def _body(emulator: CloudRunEmulator, service: str) -> dict[str, Any]:
    return emulator.services[service].body


async def test_service_is_internal_owns_an_identity_and_egresses_into_the_cell(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    spec = new_spec(FIRST)
    rev = await cloud_run.apply(spec)
    body = _body(emulator, spec.service)
    assert body["ingress"] == "INGRESS_TRAFFIC_INTERNAL_ONLY"
    assert body["invokerIamDisabled"] is False
    template = body["template"]
    assert template["revision"] == rev
    assert template["serviceAccount"] == f"{spec.service}@{PROJECT}.iam.gserviceaccount.com"
    assert emulator.accounts[spec.service]["email"] == template["serviceAccount"]
    assert template["vpcAccess"] == {
        "egress": "ALL_TRAFFIC",
        "networkInterfaces": [{"network": CELL.network, "subnetwork": CELL.subnetwork}],
    }
    (container,) = template["containers"]
    assert container["image"] == f"{CELL.image_repository}@{FIRST}"
    assert container["resources"]["limits"] == {"cpu": "1", "memory": "512Mi"}
    assert container["startupProbe"]["httpGet"] == {"path": "/healthz", "port": 8080}
    assert emulator.policy(spec.service) == {
        "bindings": [{"role": "roles/run.invoker", "members": [f"serviceAccount:{GATEWAY}"]}]
    }


@pytest.mark.parametrize(
    ("size", "cpu", "memory"),
    [("small", "1", "512Mi"), ("medium", "1", "2048Mi"), ("large", "2", "4096Mi")],
)
async def test_resource_classes_and_scaling(
    cloud_run: CloudRunDriver,
    emulator: CloudRunEmulator,
    size: ResourceClassName,
    cpu: str,
    memory: str,
) -> None:
    spec = replace(new_spec(FIRST), resource_class=size, min_instances=1, max_instances=1)
    await cloud_run.apply(spec)
    emulator.settle()
    body = _body(emulator, spec.service)
    assert body["template"]["containers"][0]["resources"]["limits"] == {
        "cpu": cpu,
        "memory": memory,
    }
    seen = await observed(cloud_run, spec.service)
    assert seen.revisions[0].spec_fingerprint == spec.spec_fingerprint
    assert body["scaling"] == {
        "scalingMode": "AUTOMATIC",
        "minInstanceCount": 1,
        "maxInstanceCount": 1,
    }


@pytest.mark.parametrize(
    ("billing", "seconds", "concurrency", "cpu_idle"),
    [("instance", 3600, 1000, False), ("request", 300, 80, True)],
)
async def test_billing_timeout_and_concurrency_reach_the_revision(  # noqa: PLR0913  (fixtures)
    cloud_run: CloudRunDriver,
    emulator: CloudRunEmulator,
    billing: Billing,
    seconds: int,
    concurrency: int,
    cpu_idle: bool,
) -> None:
    spec = replace(
        new_spec(FIRST),
        billing=billing,
        timeout_seconds=seconds,
        concurrency=concurrency,
        max_instances=1,
    )
    rev = await cloud_run.apply(spec)
    emulator.settle()
    template = _body(emulator, spec.service)["template"]
    assert template["timeout"] == f"{seconds}s"
    assert template["maxInstanceRequestConcurrency"] == concurrency
    assert template["containers"][0]["resources"]["cpuIdle"] is cpu_idle
    assert revision(await observed(cloud_run, spec.service), rev).spec_fingerprint == (
        spec.spec_fingerprint
    )


async def test_cpu_idle_left_out_reads_as_instance_billing(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    spec = replace(new_spec(FIRST), billing="instance", timeout_seconds=3600)
    await cloud_run.apply(spec)
    emulator.settle()
    for r in emulator.services[spec.service].revisions:
        del r["containers"][0]["resources"]["cpuIdle"]
    (seen,) = (await observed(cloud_run, spec.service)).revisions
    assert seen.spec_fingerprint == spec.spec_fingerprint


async def test_concurrency_left_out_reads_as_80_and_needs_no_new_revision(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    spec = new_spec(FIRST)
    rev = await cloud_run.apply(spec)
    emulator.settle()
    for r in emulator.services[spec.service].revisions:
        del r["maxInstanceRequestConcurrency"]
    (seen,) = (await observed(cloud_run, spec.service)).revisions
    assert seen.spec_fingerprint == spec.spec_fingerprint
    assert (await reconcile_once(cloud_run, spec)).change is None
    assert traffic(await observed(cloud_run, spec.service)) == {rev: 100}


@pytest.mark.parametrize(
    "edit",
    [
        lambda t: t["containers"][0]["resources"].update(cpuIdle=True),
        lambda t: t.update(timeout="300s"),
        lambda t: t.update(maxInstanceRequestConcurrency=80),
    ],
)
async def test_reconciler_repairs_billing_timeout_and_concurrency_drift(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator, edit: Callable[[Any], None]
) -> None:
    spec = replace(
        new_spec(FIRST), billing="instance", timeout_seconds=3600, concurrency=1000, max_instances=1
    )
    rev = await cloud_run.apply(spec)
    emulator.settle()

    def console_edit(body: dict[str, Any]) -> None:
        body["template"]["revision"] = None
        edit(body["template"])
        body["traffic"] = [{"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 100}]

    emulator.edit(spec.service, console_edit)
    (serving,) = [
        r for r in (await observed(cloud_run, spec.service)).revisions if r.traffic_percent == 100
    ]
    assert serving.spec_fingerprint != spec.spec_fingerprint
    kinds = [(await reconcile_once(cloud_run, spec)).change for _ in range(3)]
    assert [c.kind if c else None for c in kinds] == ["set_traffic", None, None]
    assert traffic(await observed(cloud_run, spec.service)) == {rev: 100}


async def test_invoker_policy_is_restored_on_apply(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    spec = new_spec(FIRST)
    await cloud_run.apply(spec)
    emulator.services[spec.service].policy = {
        "bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]
    }
    await cloud_run.apply(spec)
    assert emulator.policy(spec.service) == {
        "bindings": [{"role": "roles/run.invoker", "members": [f"serviceAccount:{GATEWAY}"]}]
    }


async def test_identity_is_created_once(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    spec = new_spec(FIRST)
    await cloud_run.apply(spec)
    emulator.settle()
    await cloud_run.apply(replace(spec, image_digest=SECOND))
    creates = [c for c in emulator.calls if c[1].endswith("/serviceAccounts")]
    assert len(creates) == 1


async def test_a_new_identity_is_waited_for_until_cloud_run_may_act_as_it(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    spec = new_spec(FIRST)
    emulator.new_accounts_unusable_for(3)
    rev = await cloud_run.apply(spec)
    assert emulator.services[spec.service].body["template"]["revision"] == rev
    creates = [c for c in emulator.calls if c == ("POST", f"/v2/{CELL.parent}/services")]
    assert len(creates) == 4


async def test_an_identity_that_never_settles_is_an_error(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    emulator.new_accounts_unusable_for(100)
    with pytest.raises(RuntimeDriverError, match="still not usable"):
        await cloud_run.apply(new_spec(FIRST))


async def test_refuses_names_outside_ssc(cloud_run: CloudRunDriver) -> None:
    spec = replace(new_spec(FIRST), service="ssc-a-not-an-env")
    with pytest.raises(RuntimeDriverError, match="not an SSC app"):
        await cloud_run.apply(spec)
    with pytest.raises(RuntimeDriverError, match="not an SSC app"):
        await cloud_run.observe("ssc-gateway")


async def test_port_is_left_to_cloud_run(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    spec = new_spec(FIRST)
    await cloud_run.apply(spec)
    emulator.settle()
    container = emulator.services[spec.service].body["template"]["containers"][0]
    assert "PORT" not in {var["name"] for var in container["env"]}
    assert container["ports"] == [{"containerPort": spec.port}]
    for env, match in (
        ({**spec.env, "PORT": "9000"}, "PORT must be the container port"),
        ({k: v for k, v in spec.env.items() if k != "PORT"}, "PORT must be"),
        ({**spec.env, "K_SERVICE": "x"}, "Cloud Run sets K_SERVICE"),
    ):
        with pytest.raises(RuntimeDriverError, match=match):
            await cloud_run.apply(replace(spec, env=env))


async def test_failed_revision_reports_failed(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    spec = new_spec(FIRST)
    old = await cloud_run.apply(spec)
    emulator.settle()
    emulator.unhealthy(SECOND)
    new = await cloud_run.apply(replace(spec, image_digest=SECOND))
    emulator.settle()
    seen = await observed(cloud_run, spec.service)
    by_name = {r.revision: r for r in seen.revisions}
    assert (by_name[new].ready, by_name[new].failed) == (False, True)
    assert traffic(seen) == {old: 100}


async def test_an_image_index_is_refused_once_cloud_run_resolves_it(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    spec = new_spec(FIRST)
    emulator.index(FIRST, SECOND)
    await cloud_run.apply(spec)
    emulator.settle()
    seen = await observed(cloud_run, spec.service)
    assert [r.image_digest for r in seen.revisions] == [SECOND]
    with pytest.raises(RuntimeDriverError, match=f"ran {SECOND} for {FIRST}"):
        await cloud_run.apply(spec)


async def test_drift_outside_ssc_shows_and_is_repaired(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    spec = new_spec(FIRST)
    rev = await cloud_run.apply(spec)
    emulator.settle()

    def console_edit(body: dict[str, Any]) -> None:
        body["template"]["revision"] = None
        body["template"]["containers"][0]["env"].append({"name": "DEBUG", "value": "1"})
        body["traffic"] = [{"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 100}]
        body["scaling"]["maxInstanceCount"] = 9

    emulator.edit(spec.service, console_edit)
    seen = await observed(cloud_run, spec.service)
    (serving,) = [r for r in seen.revisions if r.traffic_percent == 100]
    assert serving.revision != rev
    assert serving.spec_fingerprint != spec.spec_fingerprint
    assert seen.max_instances == 9

    assert await cloud_run.apply(spec) == rev
    await cloud_run.set_traffic(spec.service, rev)
    seen = await observed(cloud_run, spec.service)
    assert traffic(seen) == {rev: 100}
    assert seen.max_instances == spec.max_instances


async def test_a_concurrent_write_is_retried(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    spec = new_spec(FIRST)
    await cloud_run.apply(spec)
    emulator.settle()
    handler = emulator.handler
    raced = False

    def race(request: httpx2.Request) -> httpx2.Response:
        nonlocal raced
        if request.method == "PATCH" and not raced:
            raced = True
            emulator.edit(spec.service, lambda body: body["labels"].update(other="x"))
        return handler(request)

    cloud_run._client = httpx2.AsyncClient(transport=httpx2.MockTransport(race))  # pyright: ignore[reportPrivateUsage]
    await cloud_run.apply(replace(spec, max_instances=5))
    emulator.settle()
    assert raced
    assert (await observed(cloud_run, spec.service)).max_instances == 5


async def test_set_traffic_waits_for_cloud_run(emulator: CloudRunEmulator) -> None:
    slept: list[float] = []

    async def token() -> str:
        return "access-token"

    async def tick(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            emulator.settle()

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(emulator.handler))
    driver = CloudRunDriver(CELL, token, client=client, sleep=tick, poll_seconds=0.5)
    spec = new_spec(FIRST)
    rev = await driver.apply(spec)
    emulator.settle()
    await driver.set_traffic(spec.service, rev)
    assert slept == [0.5, 0.5, 0.5]
    emulator.settle = lambda: None  # type: ignore[method-assign]
    await driver.apply(replace(spec, max_instances=4))
    with pytest.raises(RuntimeDriverError, match="not settled"):
        await driver.wait_settled(spec.service, within=1.0)


# ── the agent's HTTP surface ─────────────────────────────────────────────────


async def test_agent_refuses_bad_requests(cloud_run: CloudRunDriver) -> None:
    transport = httpx2.ASGITransport(app=create_app(cloud_run, org_id=CONTRACT_ORG))
    headers = {ORG_HEADER: CONTRACT_ORG}
    async with httpx2.AsyncClient(transport=transport, base_url=AGENT, headers=headers) as client:
        bad_name = await client.post("/v1/runtime/observe", json={"service": "ssc-gateway"})
        assert (bad_name.status_code, bad_name.json()["code"]) == (400, "INVALID_REQUEST")
        not_json = await client.post("/v1/runtime/observe", content=b"[]")
        assert not_json.status_code == 400
        tampered = new_spec(FIRST)
        wire = {
            "spec": {
                "service": tampered.service,
                "image_digest": FIRST,
                "port": 8080,
                "health_path": "/healthz",
                "resource_class": "small",
                "env": {},
                "billing": "request",
                "timeout_seconds": 300,
                "concurrency": 80,
                "min_instances": 0,
                "max_instances": 1,
                "labels": {},
                "spec_fingerprint": "sha256:" + "0" * 64,
            }
        }
        refused = await client.post("/v1/runtime/apply", json=wire)
        assert refused.status_code == 400
        unknown = await client.post("/v1/runtime/delete", json={})
        assert unknown.status_code == 404
        assert (await client.get("/healthz")).json() == {"status": "ok"}


async def test_agent_errors_map_back_to_driver_errors(cloud_run: CloudRunDriver) -> None:
    async def id_token(_: str) -> str:
        return "id-token"

    transport = httpx2.ASGITransport(app=create_app(cloud_run, org_id=CONTRACT_ORG))
    driver = CellAgentDriver(
        AGENT, id_token, org_id=CONTRACT_ORG, client=httpx2.AsyncClient(transport=transport)
    )
    with pytest.raises(ServiceNotFoundError):
        await driver.scale_to_zero(new_spec(FIRST).service)
    with pytest.raises(RuntimeDriverError, match="INVALID_REQUEST"):
        await driver.observe("ssc-gateway")


async def test_reconciler_repairs_drift_in_two_passes(
    cloud_run: CloudRunDriver, emulator: CloudRunEmulator
) -> None:
    spec = new_spec(FIRST)
    rev = await cloud_run.apply(spec)
    emulator.settle()
    assert (await reconcile_once(cloud_run, spec)).kind == "converged"

    def console_edit(body: dict[str, Any]) -> None:
        body["template"]["revision"] = None
        body["template"]["containers"][0]["ports"] = [{"containerPort": 9000}]
        body["traffic"] = [{"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 100}]

    emulator.edit(spec.service, console_edit)
    kinds = [(await reconcile_once(cloud_run, spec)).change for _ in range(3)]
    assert [c.kind if c else None for c in kinds] == ["set_traffic", None, None]
    assert traffic(await observed(cloud_run, spec.service)) == {rev: 100}
