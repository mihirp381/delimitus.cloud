"""SSC-017: desired state, the one-change-per-pass plan, and convergence against the fake driver.

Ticket "done when" checks (the Postgres half is in test_worker.py):
  * every environment is minimum 0, no warm instances  -> test_min_instances_is_always_zero
  * max instances 1 for sessions or Streamlit (C11)   -> test_max_instances_rule
  * a session environment is instance-billed, one instance, 3600 s, concurrency 1000; any
    other is request-billed, 300 s, 80, minimum 0
                                  -> test_billing_and_timeout_follow_the_session_rule
  * billing, timeout and concurrency define the revision
                                  -> test_fingerprint_covers_the_revision_and_not_the_scaling
  * images by digest only                          -> test_images_are_pinned_by_digest_only
  * a disabled app is never started                -> test_stopped_beats_a_missing_service
  * the plan table, one change per pass            -> test_plan_table
  * any state converges within 3 passes            -> test_any_state_converges_within_three_passes
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ssc_contracts import app_database, app_env
from ssc_contracts.ids import new_id
from ssc_contracts.manifest import RESOURCE_CLASSES, Manifest
from ssc_control.runtime.driver import (
    REQUEST_CONCURRENCY,
    REQUEST_TIMEOUT_SECONDS,
    SESSION_CONCURRENCY,
    SESSION_TIMEOUT_SECONDS,
    EnvironmentRow,
    ReleaseRow,
    RevisionObservation,
    ServiceObservation,
    ServiceSpec,
    Stopped,
    desired_for,
    service_name,
)
from ssc_control.runtime.fake import FakeRuntimeDriver, changed
from ssc_control.runtime.reconciler import Change, Wait, plan_one_change

IMAGE = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
THIRD = "sha256:" + "c" * 64


def manifest(**tables: Any) -> Manifest:
    return Manifest.model_validate({"schema": "ssc/v1", **tables})


def env_row(name: str = "prod") -> EnvironmentRow:
    return EnvironmentRow(id=new_id("env"), org_id=new_id("org"), app_id=new_id("app"), name=name)


def spec_for(m: Manifest | None = None, *, name: str = "prod", image: str = IMAGE) -> ServiceSpec:
    desired = desired_for(
        env=env_row(name),
        release=ReleaseRow(id=new_id("rel"), image_digest=image),
        manifest=m or manifest(),
        app_status="active",
    )
    assert isinstance(desired, ServiceSpec)
    return desired


# ── desired state ────────────────────────────────────────────────────────────


def test_spec_carries_the_platform_env_and_labels() -> None:
    env = env_row()
    spec = desired_for(
        env=env,
        release=ReleaseRow(id=new_id("rel"), image_digest=IMAGE),
        manifest=manifest(runtime={"port": 3000, "health_path": "/healthz", "class": "medium"}),
        app_status="active",
    )
    assert isinstance(spec, ServiceSpec)
    assert spec.service == service_name(env.id) == "ssc-a-" + env.id.removeprefix("env_")
    assert len(spec.service) <= 49
    assert dict(spec.env) == {app_env.PORT: "3000", app_env.HOME: "/tmp"}
    assert (spec.port, spec.health_path, spec.resource_class) == (3000, "/healthz", "medium")
    assert spec.resources == RESOURCE_CLASSES["medium"]
    assert dict(spec.labels) == {"ssc-org": env.org_id, "ssc-app": env.app_id, "ssc-env": env.id}
    with pytest.raises(TypeError):
        spec.env["X"] = "y"


@pytest.mark.parametrize("env_name", ["prod", "preview"])
@pytest.mark.parametrize(
    "tables",
    [
        {},
        {"state": {"postgres": True}},
        {"connections": {"names": ["crm"]}},
        {"egress": {"hosts": ["api.example.com"]}},
        {"runtime": {"sessions": True}, "state": {"postgres": True}},
    ],
)
def test_min_instances_is_always_zero(env_name: str, tables: dict[str, Any]) -> None:
    assert spec_for(manifest(**tables), name=env_name).min_instances == 0


@pytest.mark.parametrize("env_name", ["prod", "preview"])
@pytest.mark.parametrize(
    ("runtime", "framework", "session"),
    [
        ({"sessions": True}, None, True),
        ({"class": "large"}, "streamlit", True),
        ({"start": "streamlit run app.py"}, None, True),
        ({"start": "gradio app.py"}, None, True),
        ({"start": "gunicorn app:server"}, "dash", True),
        ({"start": "shiny run app.py"}, None, True),
        ({}, None, False),
        ({"class": "large"}, "fastapi", False),
        ({"start": "python -m uvicorn app:app"}, None, False),
    ],
)
def test_billing_and_timeout_follow_the_session_rule(
    env_name: str, runtime: dict[str, Any], framework: str | None, session: bool
) -> None:
    desired = desired_for(
        env=env_row(env_name),
        release=ReleaseRow(id=new_id("rel"), image_digest=IMAGE),
        manifest=manifest(runtime=runtime, state={"postgres": True}),
        app_status="active",
        framework=framework,
    )
    assert isinstance(desired, ServiceSpec)
    if session:
        assert (desired.billing, desired.min_instances, desired.max_instances) == ("instance", 0, 1)
        assert desired.timeout_seconds == SESSION_TIMEOUT_SECONDS == 3600
        assert desired.concurrency == SESSION_CONCURRENCY == 1000
    else:
        assert (desired.billing, desired.min_instances) == ("request", 0)
        assert desired.max_instances == app_database.MAX_INSTANCES
        stateless = desired_for(
            env=env_row(env_name),
            release=ReleaseRow(id=new_id("rel"), image_digest=IMAGE),
            manifest=manifest(runtime=runtime),
            app_status="active",
            framework=framework,
        )
        assert isinstance(stateless, ServiceSpec)
        assert stateless.max_instances > 1
        assert desired.timeout_seconds == REQUEST_TIMEOUT_SECONDS == 300
        assert desired.concurrency == REQUEST_CONCURRENCY == 80


@pytest.mark.parametrize(
    ("runtime", "framework", "expected"),
    [
        ({"class": "small"}, None, 2),
        ({"class": "medium"}, None, 4),
        ({"class": "large"}, None, 8),
        ({"class": "large", "sessions": True}, None, 1),
        ({"class": "large"}, "streamlit", 1),
        ({"class": "large"}, "Streamlit", 1),
        ({"class": "large", "start": "streamlit run app.py --server.port $PORT"}, None, 1),
        ({"class": "large", "start": "uv run /opt/venv/bin/streamlit run app.py"}, None, 1),
        ({"class": "large", "start": "gradio app.py"}, None, 1),
        ({"class": "large", "start": "shiny run app.py --port 8080"}, None, 1),
        ({"class": "large", "start": "gunicorn app:server"}, "dash", 1),
        ({"class": "medium", "start": "python app.py"}, "gradio", 1),
        ({"class": "medium", "start": "dash -c 'node server.js'"}, None, 4),
        ({"class": "large", "start": "python -m uvicorn app:app"}, "fastapi", 8),
    ],
)
def test_max_instances_rule(runtime: dict[str, Any], framework: str | None, expected: int) -> None:
    desired = desired_for(
        env=env_row(),
        release=ReleaseRow(id=new_id("rel"), image_digest=IMAGE),
        manifest=manifest(runtime=runtime),
        app_status="active",
        framework=framework,
    )
    assert isinstance(desired, ServiceSpec)
    assert desired.max_instances == expected


@pytest.mark.parametrize(
    "image", ["python:3.14", "ghcr.io/x/y:latest", "ghcr.io/x/y@sha256:" + "a" * 64, "sha256:ab"]
)
def test_images_are_pinned_by_digest_only(image: str) -> None:
    with pytest.raises(ValueError, match="digest"):
        spec_for(image=image)


def test_spec_refuses_bad_scaling_and_foreign_names() -> None:
    spec = spec_for()
    fields = {
        "image_digest": IMAGE,
        "port": 8080,
        "health_path": "/",
        "resource_class": "small",
        "env": {},
        "labels": {},
    }
    ok: dict[str, Any] = {"billing": "request", "timeout_seconds": 300, "concurrency": 80}
    with pytest.raises(ValueError, match="min_instances"):
        ServiceSpec(service=spec.service, min_instances=2, max_instances=1, **fields, **ok)
    with pytest.raises(ValueError, match="ssc-a-"):
        ServiceSpec(service="other", min_instances=0, max_instances=1, **fields, **ok)
    for bad in (
        {"billing": "always"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 3601},
        {"concurrency": 0},
        {"concurrency": 1001},
    ):
        with pytest.raises(ValueError, match="billing|timeout_seconds|concurrency"):
            ServiceSpec(
                service=spec.service, min_instances=0, max_instances=1, **fields, **ok | bad
            )
    with pytest.raises(ValueError, match="environment id"):
        service_name(new_id("app"))


@pytest.mark.parametrize("status", ["disabled", "quarantined"])
def test_inactive_app_is_stopped(status: str) -> None:
    env = env_row()
    desired = desired_for(
        env=env,
        release=ReleaseRow(id=new_id("rel"), image_digest=IMAGE),
        manifest=manifest(),
        app_status=status,
    )
    assert desired == Stopped(service=service_name(env.id), reason=status)


def test_fingerprint_covers_the_revision_and_not_the_scaling() -> None:
    base = spec_for()
    same = ServiceSpec(
        service=base.service,
        image_digest=base.image_digest,
        port=base.port,
        health_path=base.health_path,
        resource_class=base.resource_class,
        env=dict(base.env),
        billing=base.billing,
        timeout_seconds=base.timeout_seconds,
        concurrency=base.concurrency,
        min_instances=1,
        max_instances=2,
        labels={"other": "label"},
    )
    assert same.spec_fingerprint == base.spec_fingerprint
    assert replace(base, billing="instance").spec_fingerprint != base.spec_fingerprint
    assert replace(base, timeout_seconds=3600).spec_fingerprint != base.spec_fingerprint
    assert replace(base, concurrency=1000).spec_fingerprint != base.spec_fingerprint
    assert spec_for(manifest(runtime={"sessions": True})).spec_fingerprint != base.spec_fingerprint
    for m in (
        manifest(runtime={"port": 9000}),
        manifest(runtime={"health_path": "/up"}),
        manifest(runtime={"class": "medium"}),
    ):
        assert spec_for(m).spec_fingerprint != base.spec_fingerprint
    assert spec_for(image=OTHER).spec_fingerprint != base.spec_fingerprint


def test_secret_references_define_the_revision() -> None:
    base = spec_for()
    one = replace(base, secrets={"STRIPE_KEY": "1"})
    assert one.spec_fingerprint != base.spec_fingerprint
    assert replace(base, secrets={"STRIPE_KEY": "2"}).spec_fingerprint != one.spec_fingerprint
    assert replace(base, secrets={"OTHER_KEY": "1"}).spec_fingerprint != one.spec_fingerprint
    two = {"A_KEY": "1", "B_KEY": "4"}
    assert (
        replace(base, secrets=two).spec_fingerprint
        == replace(base, secrets=dict(reversed(two.items()))).spec_fingerprint
    )
    with pytest.raises(TypeError):
        one.secrets["STRIPE_KEY"] = "9"  # type: ignore[index]


def test_desired_state_carries_the_pinned_secret_versions() -> None:
    desired = desired_for(
        env=env_row(),
        release=ReleaseRow(id=new_id("rel"), image_digest=IMAGE),
        manifest=manifest(),
        app_status="active",
        secrets={"STRIPE_KEY": "3"},
    )
    assert isinstance(desired, ServiceSpec)
    assert dict(desired.secrets) == {"STRIPE_KEY": "3"}
    assert "STRIPE_KEY" not in desired.env


@pytest.mark.parametrize(
    "secrets",
    [
        {"stripe_key": "1"},
        {"PORT": "1"},
        {"SSC_TOKEN": "1"},
        {"K_SERVICE": "1"},
        {"SSC_ENV": "1"},
        {"STRIPE_KEY": "latest"},
        {"STRIPE_KEY": "0"},
        {"STRIPE_KEY": ""},
    ],
)
def test_a_secret_reference_is_a_name_and_a_version_number(secrets: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="secret"):
        replace(spec_for(), secrets=secrets)


def test_a_secret_may_not_shadow_a_plain_variable() -> None:
    spec = spec_for()
    with pytest.raises(ValueError, match="both"):
        replace(spec, env={**spec.env, "STRIPE_KEY": "x"}, secrets={"STRIPE_KEY": "1"})


# ── the plan ─────────────────────────────────────────────────────────────────

SPEC = spec_for()


def rev(
    name: str,
    *,
    fingerprint: str = SPEC.spec_fingerprint,
    image: str = IMAGE,
    ready: bool | None = True,
    failed: bool = False,
    traffic: int = 0,
) -> RevisionObservation:
    return RevisionObservation(
        revision=name,
        spec_fingerprint=fingerprint,
        image_digest=image,
        ready=ready,
        failed=failed,
        traffic_percent=traffic,
    )


def seen(
    *revisions: RevisionObservation,
    scaling: tuple[int, int] = (SPEC.min_instances, SPEC.max_instances),
    stopped: bool = False,
) -> ServiceObservation:
    return ServiceObservation(
        service=SPEC.service,
        revisions=revisions,
        min_instances=scaling[0],
        max_instances=scaling[1],
        stopped=stopped,
    )


STOP = Stopped(service=SPEC.service, reason="disabled")
DRIFTED = spec_for(image=OTHER).spec_fingerprint


@pytest.mark.parametrize(
    ("desired", "observed", "expected"),
    [
        # 1. Stopped beats everything, including a missing service.
        (STOP, None, None),
        (STOP, seen(rev("r1", traffic=100), stopped=True), None),
        (STOP, seen(rev("r1", traffic=100)), Change("scale_to_zero")),
        # 2. missing service
        (SPEC, None, Change("apply")),
        # 3. no matching revision, stopped, or scaling drift
        (SPEC, seen(rev("r1", fingerprint=DRIFTED, image=OTHER, traffic=100)), Change("apply")),
        (SPEC, seen(rev("r1", image=OTHER, traffic=100)), Change("apply")),
        (SPEC, seen(), Change("apply")),
        (SPEC, seen(rev("r1", traffic=100), stopped=True), Change("apply")),
        (SPEC, seen(rev("r1", traffic=100), scaling=(1, 2)), Change("apply")),
        (SPEC, seen(rev("r1", traffic=100), scaling=(0, 8)), Change("apply")),
        # 4. matching revision not ready: wait, no change
        (SPEC, seen(rev("r1", ready=None)), Wait("r1", failed=False)),
        (SPEC, seen(rev("r1", ready=False, failed=True)), Wait("r1", failed=True)),
        # 5. traffic not on the matching revision
        (
            SPEC,
            seen(rev("r1"), rev("r2", fingerprint=DRIFTED, image=OTHER, traffic=100)),
            Change("set_traffic", "r1"),
        ),
        # 6. converged
        (SPEC, seen(rev("r1", traffic=100)), None),
        (SPEC, seen(rev("r0", fingerprint=DRIFTED, image=OTHER), rev("r1", traffic=100)), None),
    ],
)
def test_plan_table(
    desired: ServiceSpec | Stopped,
    observed: ServiceObservation | None,
    expected: Change | Wait | None,
) -> None:
    assert plan_one_change(desired, observed) == expected


def test_plan_prefers_the_matching_revision_with_traffic() -> None:
    observed = seen(rev("r1"), rev("r2", traffic=100))
    assert plan_one_change(SPEC, observed) is None


def test_stopped_beats_a_missing_service() -> None:
    assert plan_one_change(STOP, None) is None  # a disabled app is never created or restarted
    assert plan_one_change(STOP, seen(rev("r1", traffic=100))) == Change("scale_to_zero")


# ── convergence against the fake ─────────────────────────────────────────────


async def execute(driver: FakeRuntimeDriver, desired: ServiceSpec | Stopped) -> int:
    """Run passes until converged; return how many passes it took (the last one sees None)."""
    for passes in range(1, 10):
        plan = plan_one_change(desired, await driver.observe(desired.service))
        match plan:
            case None:
                return passes
            case Wait():
                raise AssertionError(f"waiting on a healthy image: {plan}")
            case Change(kind="apply"):
                assert isinstance(desired, ServiceSpec)
                await driver.apply(desired)
            case Change(kind="set_traffic", revision=str(revision)):
                await driver.set_traffic(desired.service, revision)
            case Change(kind="scale_to_zero"):
                await driver.scale_to_zero(desired.service)
            case _:
                raise AssertionError(plan)
    raise AssertionError("did not converge")


DRIFTS = st.one_of(
    st.fixed_dictionaries({"image_digest": st.sampled_from([OTHER, THIRD, IMAGE])}),
    st.fixed_dictionaries({"port": st.sampled_from([3000, 8080])}),
    st.fixed_dictionaries({"env": st.just({"PORT": "1", "HOME": "/"})}),
    st.fixed_dictionaries({"resource_class": st.sampled_from(["small", "large"])}),
    st.fixed_dictionaries({"billing": st.sampled_from(["instance", "request"])}),
    st.fixed_dictionaries({"timeout_seconds": st.sampled_from([300, 3600])}),
    st.fixed_dictionaries({"concurrency": st.sampled_from([80, 1000])}),
    st.fixed_dictionaries({"min_instances": st.integers(0, 1), "max_instances": st.integers(1, 8)}),
    st.fixed_dictionaries({"stopped": st.booleans()}),
    st.just({"delete_revision": True}),
)


@settings(max_examples=300, deadline=None)
@given(
    start=st.sampled_from(["missing", "current", "previous"]),
    drifts=st.lists(DRIFTS, max_size=6),
    stopped=st.booleans(),
    others=st.sampled_from(["healthy", "unhealthy", "starting"]),
)
def test_any_state_converges_within_three_passes(
    start: str, drifts: list[dict[str, Any]], stopped: bool, others: str
) -> None:
    async def go() -> None:
        driver = FakeRuntimeDriver()
        for digest in (OTHER, THIRD):
            getattr(driver, others)(digest)
        if start != "missing":
            await driver.apply(SPEC if start == "current" else replace(SPEC, image_digest=OTHER))
            for drift in drifts:
                svc = driver.services[SPEC.service]
                if "delete_revision" not in drift:
                    driver.drift(SPEC.service, **drift)
                elif len(svc.revisions) > 1:
                    driver.delete_revision(SPEC.service, svc.revisions[0].name)
        desired = STOP if stopped else SPEC
        assert await execute(driver, desired) <= 3
        assert await execute(driver, desired) == 1  # converged stays converged

    asyncio.run(go())


async def test_new_image_takes_apply_then_set_traffic() -> None:
    driver = FakeRuntimeDriver()
    await driver.apply(replace(SPEC, image_digest=OTHER))
    driver.reset_calls()
    assert await execute(driver, SPEC) == 3
    assert changed(driver.calls, SPEC.service) == ["apply", "set_traffic"]
    observed = await driver.observe(SPEC.service)
    assert observed is not None
    serving = [r for r in observed.revisions if r.traffic_percent == 100]
    assert [r.image_digest for r in serving] == [IMAGE]


async def test_disabled_app_is_scaled_to_zero_once_and_stays_down() -> None:
    driver = FakeRuntimeDriver()
    await driver.apply(SPEC)
    driver.reset_calls()
    assert await execute(driver, STOP) == 2
    assert await execute(driver, STOP) == 1
    assert changed(driver.calls, SPEC.service) == ["scale_to_zero"]
