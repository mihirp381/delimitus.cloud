"""The nightly runtime check in a staging cell (SSC-017): ``python -m ssc_conformance.nightly``.

1. Deploys the two probe apps through the cell agent, as the control plane does: the reconciler
   (``reconcile_once``) drives ``CellAgentDriver`` until each has converged.
2. Runs the cell's ``ssc-probe-runner`` job, which calls probe app ``a`` from where the gateway
   stands, and reads its seventeen results from Cloud Logging.
3. Drifts probe app ``a`` (a revision with an extra variable takes all traffic) and runs the
   reconciler at its tick until the service is back on the desired revision. The ticket allows
   one minute, counted from the drift to the converged observation.

Configuration, all required: ``SSC_PROBE_PROJECT`` (the cell project), ``SSC_PROBE_AGENT_URL``,
and ``SSC_PROBE_DIGEST`` (the probe image in the cell's ``ssc-apps/apps`` repository).
``SSC_CONTROL_SA`` names the identity the agent accepts; the run mints its ID tokens. Without
it, an operator calls the agent as themselves. The caller's access token comes from
``SSC_ACCESS_TOKEN`` (the nightly workflow's federated token) or else ``gcloud``; it needs the
probe job, read access to its logs, and ``getOpenIdToken`` on the control SA when one is named.

``cannot_reach_peer_cell`` and ``deny_peer_cell`` need a second cell: ``SSC_PROBE_PEER_APP_URL``,
``SSC_PROBE_PEER_GATEWAY_URL``, ``SSC_PROBE_PEER_RANGE`` and ``SSC_PROBE_PEER_PROJECT``, all or
none. Set, they reach the job as overrides (which needs ``run.jobs.runWithOverrides``, held by
``ssc-nightly`` on the probe job only) and both probes must pass. Unset, the job reports them
skipped (``no peer cell``), which is not a failure of this run: ``ssc_conformance.matrix`` decides
whether a night may go without a peer.
``datagw_read_only`` needs the cell's data gateway: ``SSC_PROBE_DATAGW_URL`` and
``SSC_PROBE_DATAGW_CONNECTION`` (a ``con_<20>`` connection id), both or none, which reach the job
the same way. Unset, the probe is skipped (``waits for the data gateway``), also not a failure.
Optionally ``SSC_PROBE_TLS_HOST`` (SSC-062): a host under the cell's apps domain. The run opens a
verified TLS connection to it and fails when the cell's wildcard certificate does not verify or
has under ``CERT_MIN_DAYS`` days left. A certificate that fails to renew keeps serving until it
expires, so this catches a failed renewal at least 14 days ahead. Unset, the check is skipped.
Exits 1 when a probe fails or is missing, or when the drift outlives the minute, or when that
check fails. ``SSC_EVIDENCE_FILE`` names the file this run adds its results to for the one-page
result (``ssc_conformance.evidence``); the cell is named by its project.
"""

import asyncio
import logging
import os
import ssl
import subprocess  # noqa: S404
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, cast

import httpx2

from ssc_conformance import evidence
from ssc_conformance.runtime_probes import PROBES
from ssc_contracts import app_env
from ssc_control.runtime.cell_agent import AccessTokens, CellAgentDriver, ImpersonatedIdTokens
from ssc_control.runtime.driver import REQUEST_CONCURRENCY, REQUEST_TIMEOUT_SECONDS
from ssc_control.runtime.jobs import TICK_CRON
from ssc_control.runtime.reconciler import Outcome, plan_one_change, reconcile_once
from ssc_shared.runtime import RuntimeDriver, RuntimeDriverError, ServiceSpec, service_name

log = logging.getLogger("ssc_conformance.nightly")

ENV: Final = {
    "project": "SSC_PROBE_PROJECT",
    "agent_url": "SSC_PROBE_AGENT_URL",
    "probe_digest": "SSC_PROBE_DIGEST",
}
CONTROL_SA_ENV: Final = "SSC_CONTROL_SA"
TLS_HOST_ENV: Final = "SSC_PROBE_TLS_HOST"
CERT_MIN_DAYS: Final = 21.0
TLS_TIMEOUT_SECONDS: Final = 15.0
PEER_CELL_ENV: Final = {
    "SSC_PROBE_PEER_APP_URL": "PROBE_PEER_CELL_APP_URL",
    "SSC_PROBE_PEER_GATEWAY_URL": "PROBE_PEER_CELL_GATEWAY_URL",
    "SSC_PROBE_PEER_RANGE": "PROBE_PEER_CELL_RANGE",
    "SSC_PROBE_PEER_PROJECT": "PROBE_PEER_CELL_PROJECT",
}
DATAGW_URL_ENV: Final = ("SSC_PROBE_DATAGW_URL", "PROBE_DATAGW_URL")
DATAGW_CONNECTION_ENV: Final = ("SSC_PROBE_DATAGW_CONNECTION", "PROBE_DATAGW_CONNECTION")
PEER_CELL_PROBES: Final = frozenset({"cannot_reach_peer_cell", "deny_peer_cell"})
DATAGW_PROBE: Final = "datagw_read_only"
PROBE_ENVS: Final = ("env_probe00000000000000a", "env_probe00000000000000b")
PROBE_JOB: Final = "ssc-probe-runner"
REGION: Final = "us-central1"
RUN_API: Final = "https://run.googleapis.com/v2"
LOGGING_API: Final = "https://logging.googleapis.com/v2"
PORT: Final = 8080
TICK_SECONDS: Final = int(TICK_CRON.rsplit("*/", 1)[1])
DRIFT_LIMIT_SECONDS: Final = 60.0
DEPLOY_LIMIT_SECONDS: Final = 600.0
JOB_LIMIT_SECONDS: Final = 900.0
LOGS_LIMIT_SECONDS: Final = 300.0
POLL_SECONDS: Final = 5.0
DRIFT_VARIABLE: Final = "PROBE_DRIFT"

type Sleep = Callable[[float], Awaitable[None]]
type Clock = Callable[[], float]
type DaysLeft = Callable[[str], Awaitable[float]]
type Json = dict[str, Any]


class NightlyError(Exception):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class NightlyConfig:
    project: str
    agent_url: str
    probe_digest: str
    control_sa: str | None
    peer_cell: Mapping[str, str] | None
    tls_host: str | None = None
    datagw_url: str | None = None
    datagw_connection: str | None = None


def config_from_env(environ: Mapping[str, str]) -> NightlyConfig:
    missing = [name for name in ENV.values() if not environ.get(name)]
    if missing:
        raise NightlyError(f"missing {', '.join(missing)}")
    peer = {job: environ[name] for name, job in PEER_CELL_ENV.items() if environ.get(name)}
    if peer and len(peer) != len(PEER_CELL_ENV):
        raise NightlyError(f"set all of {', '.join(PEER_CELL_ENV)} or none")
    datagw = environ.get(DATAGW_URL_ENV[0]) or None, environ.get(DATAGW_CONNECTION_ENV[0]) or None
    if (datagw[0] is None) != (datagw[1] is None):
        raise NightlyError(
            f"set both {DATAGW_URL_ENV[0]} and {DATAGW_CONNECTION_ENV[0]} or neither"
        )
    return NightlyConfig(
        **{field: environ[name] for field, name in ENV.items()},
        control_sa=environ.get(CONTROL_SA_ENV) or None,
        peer_cell=peer or None,
        tls_host=environ.get(TLS_HOST_ENV) or None,
        datagw_url=datagw[0],
        datagw_connection=datagw[1],
    )


def probe_spec(env_id: str, digest: str) -> ServiceSpec:
    return ServiceSpec(
        service=service_name(env_id),
        image_digest=digest,
        port=PORT,
        health_path="/health",
        resource_class="small",
        env={app_env.PORT: str(PORT), app_env.HOME: app_env.HOME_VALUE},
        billing="request",
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        concurrency=REQUEST_CONCURRENCY,
        min_instances=0,
        max_instances=1,
        labels={"ssc-env": env_id, "ssc-probe": "true"},
    )


async def converge(  # noqa: PLR0913  (keyword-only)
    driver: RuntimeDriver,
    desired: ServiceSpec,
    *,
    every: float,
    limit: float,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
    first_after: float = 0.0,
) -> float:
    """Reconcile passes ``every`` seconds until converged; returns the seconds until the service
    first observed as desired, which can be right after a pass's change."""
    started = clock()
    await sleep(first_after)
    while True:
        outcome = await reconcile_once(driver, desired)
        if outcome.kind == "changed" and await _converged(driver, desired):
            outcome = Outcome("converged", desired.service)
        elapsed = clock() - started
        if outcome.kind == "converged":
            return elapsed
        if outcome.kind == "revision_failed":
            raise NightlyError(f"{desired.service}: the desired revision failed to start")
        if elapsed > limit:
            raise NightlyError(f"{desired.service}: not converged after {elapsed:.0f} s")
        await sleep(every)


async def _converged(driver: RuntimeDriver, desired: ServiceSpec) -> bool:
    return plan_one_change(desired, await driver.observe(desired.service)) is None


async def drift(
    driver: RuntimeDriver,
    desired: ServiceSpec,
    *,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> float:
    """Moves all traffic to a revision the control plane never asked for, then times the
    reconciler at its tick (the first pass one tick later, the worst case) until repaired."""
    drifted = replace(desired, env={**desired.env, DRIFT_VARIABLE: str(int(time.time()))})
    await converge(
        driver, drifted, every=POLL_SECONDS, limit=DEPLOY_LIMIT_SECONDS, sleep=sleep, clock=clock
    )
    return await converge(
        driver,
        desired,
        every=TICK_SECONDS,
        limit=DRIFT_LIMIT_SECONDS,
        sleep=sleep,
        clock=clock,
        first_after=TICK_SECONDS,
    )


class ProbeJob:
    """The cell's probe runner job, and its results from Cloud Logging."""

    def __init__(
        self,
        project: str,
        access_tokens: AccessTokens,
        *,
        client: httpx2.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self._project = project
        self._access_tokens = access_tokens
        self._client = client or httpx2.AsyncClient(timeout=30.0)
        self._sleep = sleep
        self._clock = clock

    async def aclose(self) -> None:
        await self._client.aclose()

    async def run(self, env: Mapping[str, str] | None = None) -> list[Json]:
        """Runs the job, with ``env`` added to its container when given."""
        job = f"projects/{self._project}/locations/{REGION}/jobs/{PROBE_JOB}"
        variables = [{"name": k, "value": v} for k, v in sorted((env or {}).items())]
        body: Json = {"overrides": {"containerOverrides": [{"env": variables}]}} if env else {}
        operation = await self._call("POST", f"{RUN_API}/{job}:run", body)
        execution = str(_obj(operation.get("metadata")).get("name") or "")
        if "/executions/" not in execution:
            raise NightlyError(f"{PROBE_JOB}: the run named no execution")
        finished = await self._finished(execution)
        since = str(finished.get("createTime") or "")
        return await self._results(execution.rsplit("/", 1)[1], since)

    async def _finished(self, execution: str) -> Json:
        started = self._clock()
        while True:
            body = await self._call("GET", f"{RUN_API}/{execution}")
            if body.get("completionTime"):
                return body
            if self._clock() - started > JOB_LIMIT_SECONDS:
                raise NightlyError(f"{execution}: still running after {JOB_LIMIT_SECONDS:.0f} s")
            await self._sleep(POLL_SECONDS)

    async def _results(self, execution: str, since: str) -> list[Json]:
        """The job's ``ssc_probe`` lines once its summary line has been ingested."""
        found = (
            f'resource.type="cloud_run_job" AND resource.labels.job_name="{PROBE_JOB}" '
            f'AND labels."run.googleapis.com/execution_name"="{execution}"'
        )
        query: Json = {
            "resourceNames": [f"projects/{self._project}"],
            "filter": f'{found} AND timestamp>="{since}"' if since else found,
            "orderBy": "timestamp asc",
            "pageSize": 200,
        }
        started = self._clock()
        while True:
            payloads = [_obj(e.get("jsonPayload")) for e in await self._entries(query)]
            if any("ssc_probe_summary" in p for p in payloads):
                return [_obj(p["ssc_probe"]) for p in payloads if "ssc_probe" in p]
            if self._clock() - started > LOGS_LIMIT_SECONDS:
                raise NightlyError(f"{execution}: no probe summary in the logs")
            await self._sleep(POLL_SECONDS)

    async def _entries(self, query: Json) -> list[Json]:
        """Every page: Logging may answer a page with no entries and a token for the next."""
        entries: list[Json] = []
        token = ""
        while True:
            page = query | {"pageToken": token} if token else query
            body = await self._call("POST", f"{LOGGING_API}/entries:list", page)
            entries.extend(_objs(body.get("entries")))
            token = str(body.get("nextPageToken") or "")
            if not token:
                return entries

    async def _call(self, method: str, url: str, body: Json | None = None) -> Json:
        token = await self._access_tokens()
        try:
            response = await self._client.request(
                method, url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx2.HTTPError as exc:
            raise NightlyError(f"{method} {url}: {type(exc).__name__}") from None
        if not response.is_success:
            raise NightlyError(f"{method} {url}: HTTP {response.status_code} {response.text[:300]}")
        return _obj(response.json())


def verdict(results: list[Json], *, peer_cell: bool = False, datagw: bool = False) -> list[str]:
    """Every probe must report, exactly once, and pass. Without a peer cell the two peer-cell
    probes may report skipped (``no peer cell``) instead, and without a data gateway
    ``datagw_read_only`` may (``waits for the data gateway``); no other skip is accepted.
    Returns the failures."""
    by_name: dict[str, list[Json]] = {}
    for result in results:
        by_name.setdefault(str(result.get("probe")), []).append(result)
    failures = [f"{name}: did not report" for name in PROBES if name not in by_name]
    failures += [f"{name}: unknown probe" for name in by_name if name not in PROBES]
    failures += [f"{name}: reported {len(r)} times" for name, r in by_name.items() if len(r) > 1]
    failures += [
        f"{name}: {r[0].get('reason')}"
        for name, r in by_name.items()
        if name in PROBES
        and r[0].get("status") != "passed"
        and not _allowed_skip(name, r[0], peer_cell=peer_cell, datagw=datagw)
    ]
    return failures


def _allowed_skip(name: str, result: Json, *, peer_cell: bool, datagw: bool) -> bool:
    if result.get("status") != "skipped":
        return False
    if name in PEER_CELL_PROBES:
        return not peer_cell and result.get("reason") == evidence.NO_PEER
    return name == DATAGW_PROBE and not datagw and result.get("reason") == evidence.WAITS_FOR_DATAGW


@dataclass(frozen=True, slots=True)
class Report:
    results: list[Json]
    drift_seconds: float | None
    failures: list[str]
    checks: tuple[evidence.Result, ...] = ()

    def as_evidence(self, cell: str, *, peer: bool) -> evidence.Evidence:
        """The probes and the checks of this run, as the one-page result reads them."""
        found = (*evidence.probe_results(self.results), *self.checks)
        return evidence.Evidence(cell, peer, found)

    def markdown(self) -> str:
        lines = ["| probe | status | reason |", "| --- | --- | --- |"]
        lines += [
            f"| {r.get('probe')} | {r.get('status')} | {str(r.get('reason')).replace('|', '/')} |"
            for r in self.results
        ]
        repaired = "not run" if self.drift_seconds is None else f"{self.drift_seconds:.0f} s"
        lines += ["", f"Drift repaired in: {repaired} (limit {DRIFT_LIMIT_SECONDS:.0f} s)"]
        lines += ["", *(f"- FAILED {f}" for f in self.failures)] if self.failures else []
        return "\n".join(lines) + "\n"


async def certificate_days_left(
    host: str,
    *,
    port: int = 443,
    context: ssl.SSLContext | None = None,
    now: Callable[[], float] = time.time,
) -> float:
    """Days until the certificate ``host`` serves expires, over a connection that verifies the
    chain and the name. A connection that does not verify raises ``NightlyError``."""
    try:
        async with asyncio.timeout(TLS_TIMEOUT_SECONDS):
            _, writer = await asyncio.open_connection(
                host, port, ssl=context or ssl.create_default_context(), server_hostname=host
            )
    except (OSError, TimeoutError) as exc:
        raise NightlyError(f"{host} does not verify: {type(exc).__name__}: {exc}") from exc
    try:
        ssl_object = cast("ssl.SSLObject", writer.get_extra_info("ssl_object"))
        expires = ssl.cert_time_to_seconds(str(ssl_object.getpeercert()["notAfter"]))  # pyright: ignore[reportOptionalSubscript]
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
    return (expires - now()) / 86400


async def certificate_failure(host: str, days_left: DaysLeft = certificate_days_left) -> str | None:
    """Why the certificate at ``host`` fails the night, or None when it has ``CERT_MIN_DAYS`` or
    more days left."""
    try:
        days = await days_left(host)
    except NightlyError as exc:
        return f"certificate: {exc}"
    if days < CERT_MIN_DAYS:
        return (
            f"certificate: {host} expires in {days:.0f} days, under {CERT_MIN_DAYS:.0f}: "
            "its renewal has failed"
        )
    return None


async def nightly(  # noqa: PLR0913  (keyword-only)
    driver: RuntimeDriver,
    job: ProbeJob,
    digest: str,
    *,
    peer_cell: Mapping[str, str] | None = None,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
    tls_host: str | None = None,
    days_left: DaysLeft = certificate_days_left,
    datagw_url: str | None = None,
    datagw_connection: str | None = None,
) -> Report:
    specs = [probe_spec(env_id, digest) for env_id in PROBE_ENVS]
    for spec in specs:
        await converge(
            driver, spec, every=POLL_SECONDS, limit=DEPLOY_LIMIT_SECONDS, sleep=sleep, clock=clock
        )
        log.info("probe app ready", extra={"service": spec.service})
    datagw = bool(datagw_url and datagw_connection)
    overrides = {
        **(peer_cell or {}),
        **(
            {DATAGW_URL_ENV[1]: datagw_url, DATAGW_CONNECTION_ENV[1]: datagw_connection}
            if datagw
            else {}
        ),
    }
    results = await job.run(overrides or None)
    failures = verdict(results, peer_cell=peer_cell is not None, datagw=datagw)
    checks: list[evidence.Result] = []
    drift_seconds: float | None = None
    try:
        drift_seconds = await drift(driver, specs[0], sleep=sleep, clock=clock)
        checks.append(evidence.Result("drift", evidence.OK, f"repaired in {drift_seconds:.0f} s"))
    except (NightlyError, RuntimeDriverError) as exc:
        failures.append(f"drift: {exc}")
        checks.append(evidence.Result("drift", evidence.FAIL, str(exc)))
    if not tls_host:
        checks.append(evidence.Result("certificate", evidence.SKIPPED, f"{TLS_HOST_ENV} not set"))
    elif problem := await certificate_failure(tls_host, days_left):
        failures.append(problem)
        checks.append(evidence.Result("certificate", evidence.FAIL, problem))
    else:
        checks.append(
            evidence.Result("certificate", evidence.OK, f"{CERT_MIN_DAYS:.0f}+ days left")
        )
    return Report(results, drift_seconds, failures, tuple(checks))


async def _gcloud(*args: str) -> str:
    done = await asyncio.to_thread(
        subprocess.run,
        ["gcloud", "auth", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise NightlyError(f"gcloud auth {args[0]} failed: log in to gcloud")
    return done.stdout.strip()


def gcloud_access_tokens() -> AccessTokens:
    """``SSC_ACCESS_TOKEN`` if set, else the operator's ``gcloud`` login. Never logged."""

    async def token() -> str:
        return os.environ.get("SSC_ACCESS_TOKEN") or await _gcloud("print-access-token")

    return token


async def operator_id_token(_audience: str) -> str:
    """The operator's own ID token, for a run without the control SA: Cloud Run accepts it
    when the operator may invoke the agent (the just-in-time ``writer`` grant on a cell)."""
    return await _gcloud("print-identity-token")


async def main_async(environ: Mapping[str, str]) -> Report:
    cfg = config_from_env(environ)
    access = gcloud_access_tokens()
    id_tokens = (
        ImpersonatedIdTokens(cfg.control_sa, access) if cfg.control_sa else operator_id_token
    )
    driver = CellAgentDriver(cfg.agent_url, id_tokens)
    job = ProbeJob(cfg.project, access)
    try:
        report = await nightly(
            driver,
            job,
            cfg.probe_digest,
            peer_cell=cfg.peer_cell,
            tls_host=cfg.tls_host,
            datagw_url=cfg.datagw_url,
            datagw_connection=cfg.datagw_connection,
        )
        if path := environ.get(evidence.EVIDENCE_ENV):
            evidence.write(
                Path(path), report.as_evidence(cfg.project, peer=cfg.peer_cell is not None)
            )
        return report
    finally:
        await driver.aclose()
        await job.aclose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        report = asyncio.run(main_async(os.environ))
    except (NightlyError, RuntimeDriverError) as exc:
        sys.stderr.write(f"nightly: {exc}\n")
        return 1
    text = report.markdown()
    sys.stdout.write(text)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as f:
            f.write("## SSC-017 runtime probes\n\n" + text)
    return 1 if report.failures else 0


def _obj(value: object) -> Json:
    return cast(Json, value) if isinstance(value, dict) else {}


def _objs(value: object) -> list[Json]:
    items = cast("list[object]", value) if isinstance(value, list) else []
    return [cast(Json, v) for v in items if isinstance(v, dict)]


if __name__ == "__main__":
    sys.exit(main())
