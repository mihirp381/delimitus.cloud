"""Compare what should run with what runs, and make at most one change per pass (SSC-017).

The order, first match wins:

1. desired ``Stopped``: nothing if the service is missing or already stopped, else
   ``scale_to_zero``. A disabled app is never created or restarted.
2. service missing: ``apply``.
3. no revision matches the spec's fingerprint, or the service is stopped, or its scaling differs:
   ``apply``.
4. the matching revision is not ready: wait (``Wait``, no change; ``failed`` says it never will).
5. traffic is not all on the matching revision: ``set_traffic``.
6. otherwise converged: ``None``.

From any state the plan converges in at most two changes, so a third pass sees ``None``.
Desired state is read from the database in a short org-bound transaction that commits before the
runtime is called: no transaction stays open across a network call.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_control.db.bind import bound_org
from ssc_control.runtime.driver import (
    EnvironmentRow,
    ReleaseRow,
    RevisionObservation,
    RuntimeDriver,
    ServiceObservation,
    ServiceSpec,
    Stopped,
    desired_for,
)
from ssc_control.runtime.specs import ReleaseSpecs, ReleaseSpecUnavailableError

log = logging.getLogger(__name__)

ChangeKind = Literal["apply", "set_traffic", "scale_to_zero"]
OutcomeKind = Literal["converged", "changed", "waiting", "revision_failed", "no_release", "no_spec"]


@dataclass(frozen=True, slots=True)
class Change:
    kind: ChangeKind
    revision: str | None = None  # set_traffic only


@dataclass(frozen=True, slots=True)
class Wait:
    revision: str
    failed: bool


def plan_one_change(
    desired: ServiceSpec | Stopped, observed: ServiceObservation | None
) -> Change | Wait | None:
    """The one next change, a wait, or None when converged. Pure."""
    if isinstance(desired, Stopped):
        return None if observed is None or observed.stopped else Change("scale_to_zero")
    if observed is None:
        return Change("apply")
    return _plan_serving(desired, observed)


def _plan_serving(desired: ServiceSpec, observed: ServiceObservation) -> Change | Wait | None:
    match = matching_revision(desired, observed)
    scaling = (observed.min_instances, observed.max_instances)
    wanted = (desired.min_instances, desired.max_instances)
    if match is None or observed.stopped or scaling != wanted:
        return Change("apply")
    if match.ready is not True:
        return Wait(match.revision, failed=match.failed)
    if match.traffic_percent != 100:
        return Change("set_traffic", match.revision)
    return None


def matching_revision(
    desired: ServiceSpec, observed: ServiceObservation
) -> RevisionObservation | None:
    """The revision that runs exactly the spec; the one with most traffic if several do."""
    matches = [
        r
        for r in observed.revisions
        if r.spec_fingerprint == desired.spec_fingerprint and r.image_digest == desired.image_digest
    ]
    return max(matches, key=lambda r: r.traffic_percent, default=None)


@dataclass(frozen=True, slots=True)
class Outcome:
    kind: OutcomeKind
    service: str | None = None
    change: Change | None = None


_DESIRED_ROWS = text(
    "select e.app_id, e.name, a.status, r.id, r.image_digest "
    "from ssc.environment e "
    "join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "join ssc.deployment d on d.org_id = e.org_id and d.id = e.current_deployment_id "
    "join ssc.release r on r.org_id = d.org_id and r.app_id = d.app_id and r.id = d.release_id "
    "where e.org_id = :org and e.id = :env"
)


async def load_desired(
    engine: AsyncEngine, specs: ReleaseSpecs, *, org_id: str, env_id: str
) -> ServiceSpec | Stopped | OutcomeKind:
    """Desired state from the database: the live pointer's release, its manifest, the app's
    status. Returns an outcome kind instead when there is nothing to reconcile."""
    async with bound_org(engine, org_id) as conn:
        row = (await conn.execute(_DESIRED_ROWS, {"org": org_id, "env": env_id})).one_or_none()
        if row is None:
            return "no_release"
        app_id, env_name, app_status, release_id, image_digest = row
        try:
            spec = await specs.get(conn, org_id=org_id, app_id=app_id, release_id=release_id)
        except ReleaseSpecUnavailableError:
            return "no_spec"
    return desired_for(
        env=EnvironmentRow(id=env_id, org_id=org_id, app_id=app_id, name=env_name),
        release=ReleaseRow(id=release_id, image_digest=image_digest),
        manifest=spec.manifest,
        app_status=app_status,
        framework=spec.framework,
    )


async def reconcile_env(
    engine: AsyncEngine,
    driver: RuntimeDriver,
    specs: ReleaseSpecs,
    *,
    org_id: str,
    env_id: str,
) -> Outcome:
    """One pass for one app environment: at most one change."""
    desired = await load_desired(engine, specs, org_id=org_id, env_id=env_id)
    if isinstance(desired, str):
        return Outcome(desired)
    observed = await driver.observe(desired.service)
    plan = plan_one_change(desired, observed)
    if plan is None:
        return Outcome("converged", desired.service)
    if isinstance(plan, Wait):
        kind: OutcomeKind = "revision_failed" if plan.failed else "waiting"
        return Outcome(kind, desired.service)
    match plan:
        case Change(kind="apply") if isinstance(desired, ServiceSpec):
            await driver.apply(desired)
        case Change(kind="set_traffic", revision=str(revision)):
            await driver.set_traffic(desired.service, revision)
        case Change(kind="scale_to_zero"):
            await driver.scale_to_zero(desired.service)
        case _:
            raise AssertionError(f"no such plan {plan} for {desired}")
    log.info(
        "reconciled",
        extra={"org_id": org_id, "env_id": env_id, "service": desired.service, "change": plan.kind},
    )
    return Outcome("changed", desired.service, plan)
