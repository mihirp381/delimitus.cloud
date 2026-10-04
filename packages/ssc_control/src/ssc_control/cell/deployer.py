"""Starting the cell deployer job and reading how a run went (SSC-087).

The deployer is the Cloud Run job ``ssc-cell-deployer`` in the platform project, with its own
service account (``infra/ssc_infra/deployer.py``). The worker holds only
``run.jobsExecutorWithOverrides`` and ``run.viewer`` on that one job: it can start a run with two
arguments, a cell label and a flag, and read the run. The flag is a lazy resource to turn on
(SSC-087) or the gateway's warm setting, ``warm=true`` or ``warm=false`` (SSC-092). It never holds
the deployer's credentials, and the job refuses any other argument or environment variable.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, cast

import httpx2

from ssc_contracts.cells import CellResource, WarmGateway
from ssc_shared.hosts import check_cell_label

type ExecutionStatus = Literal["running", "succeeded", "failed"]
type DeployerFlag = CellResource | WarmGateway

RUN_API: Final = "https://run.googleapis.com/v2"
METADATA_IDENTITY: Final = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
FAKE_JOB: Final = "projects/ssc-platform-0/locations/us-central1/jobs/ssc-cell-deployer"
CALL_TIMEOUT_SECONDS: Final = 30.0
TOKEN_MARGIN_SECONDS: Final = 300
MAX_EXECUTION_NAME: Final = 300
NOT_FOUND: Final = 404
JOB_KEYS: Final = ("projects", "locations", "jobs")

DEPLOYER_ENV: Final = "SSC_CELL_DEPLOYER"
DEPLOYER_JOB_ENV: Final = "SSC_CELL_DEPLOYER_JOB"


class CellDeployerError(RuntimeError):
    pass


class CellDeployer(Protocol):
    async def start(self, label: str, flag: DeployerFlag) -> str:
        """Start one run for ``label`` and ``flag``; the run's execution name."""
        ...

    async def status(self, execution: str) -> ExecutionStatus:
        """How the run ``execution`` stands. A run that cannot be found has failed."""
        ...


def deployer_flag(value: str) -> DeployerFlag:
    """A lazy resource or a warm setting; ``ValueError`` for anything else."""
    if value in WarmGateway:
        return WarmGateway(value)
    return CellResource(value)


def deployer_args(label: str, flag: DeployerFlag) -> list[str]:
    """The only arguments the worker ever passes: a checked cell label and a flag."""
    check_cell_label(label)
    return [label, deployer_flag(flag).value]


def execution_status(body: Mapping[str, Any]) -> ExecutionStatus:
    """A Cloud Run execution's outcome: one task, no retries."""
    if int(body.get("succeededCount") or 0) >= 1:
        return "succeeded"
    if int(body.get("failedCount") or 0) >= 1 or int(body.get("cancelledCount") or 0) >= 1:
        return "failed"
    return "failed" if body.get("completionTime") else "running"


class MetadataAccessTokens:
    """OAuth access tokens for this instance's identity, cached until shortly before expiry."""

    def __init__(self, client: httpx2.AsyncClient | None = None) -> None:
        self._client = client or httpx2.AsyncClient(timeout=5.0)
        self._token: tuple[str, float] | None = None

    async def __call__(self) -> str:
        if self._token and time.monotonic() < self._token[1]:
            return self._token[0]
        try:
            response = await self._client.get(
                METADATA_IDENTITY, headers={"Metadata-Flavor": "Google"}
            )
            response.raise_for_status()
            body = cast("dict[str, Any]", response.json())
            token, expires = str(body["access_token"]), int(body["expires_in"])
        except (httpx2.HTTPError, ValueError, KeyError) as exc:
            raise CellDeployerError(f"no access token: {type(exc).__name__}") from None
        self._token = (token, time.monotonic() + max(expires - TOKEN_MARGIN_SECONDS, 0))
        return token


class AccessTokens(Protocol):
    async def __call__(self) -> str: ...


class CloudRunCellDeployer:
    """Runs the job through the Cloud Run Admin API with the worker's own identity."""

    def __init__(
        self, job: str, tokens: AccessTokens, *, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._job = job
        self._tokens = tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def start(self, label: str, flag: DeployerFlag) -> str:
        overrides = {"containerOverrides": [{"args": deployer_args(label, flag)}]}
        body = await self._call("POST", f"{RUN_API}/{self._job}:run", {"overrides": overrides})
        metadata = cast("dict[str, Any]", (body or {}).get("metadata") or {})
        name = metadata.get("name")
        if not isinstance(name, str) or not self._owns(name):
            raise CellDeployerError("cell deployer: the run named no execution of the job")
        return name

    async def status(self, execution: str) -> ExecutionStatus:
        if not self._owns(execution):
            raise CellDeployerError("cell deployer: not an execution of the job")
        body = await self._call("GET", f"{RUN_API}/{execution}", None)
        return "failed" if body is None else execution_status(body)

    def _owns(self, execution: str) -> bool:
        return execution.startswith(f"{self._job}/executions/") and (
            len(execution) <= MAX_EXECUTION_NAME
        )

    async def _call(
        self, method: str, url: str, body: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """The JSON body; None for a GET of something that does not exist."""
        token = await self._tokens()
        try:
            response = await self._client.request(
                method, url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx2.HTTPError as exc:
            raise CellDeployerError(f"cell deployer: {type(exc).__name__}") from None
        if response.status_code == NOT_FOUND and method == "GET":
            return None
        if not response.is_success:
            raise CellDeployerError(f"cell deployer: HTTP {response.status_code}")
        try:
            payload: object = response.json()
        except ValueError:
            raise CellDeployerError("cell deployer: a body that is not JSON") from None
        return cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}


@dataclass(slots=True)
class FakeCellDeployer:
    """In memory. Each run stays ``running`` until ``finish``; ``fail_next`` fails the next runs
    as they finish, as a run killed halfway would."""

    runs: list[tuple[str, DeployerFlag]] = field(default_factory=lambda: [])
    outcomes: dict[str, ExecutionStatus] = field(default_factory=lambda: {})
    fail_next: int = 0
    refuse_start: int = 0

    async def start(self, label: str, flag: DeployerFlag) -> str:
        args = deployer_args(label, flag)
        if self.refuse_start:
            self.refuse_start -= 1
            raise CellDeployerError("cell deployer: HTTP 503")
        self.runs.append((args[0], deployer_flag(args[1])))
        name = f"{FAKE_JOB}/executions/run-{len(self.runs)}"
        self.outcomes[name] = "running"
        return name

    async def status(self, execution: str) -> ExecutionStatus:
        return self.outcomes.get(execution, "failed")

    def finish(self, execution: str | None = None) -> None:
        """Ends ``execution`` (the latest run by default): failed while ``fail_next`` lasts."""
        name = execution or next(reversed(self.outcomes))
        if self.fail_next:
            self.fail_next -= 1
            self.outcomes[name] = "failed"
        else:
            self.outcomes[name] = "succeeded"


def cell_deployer_from_env(env: Mapping[str, str]) -> CellDeployer | None:
    """``SSC_CELL_DEPLOYER``: unset means none (a lazy resource fails with
    ``CELL_DEPLOYER_UNAVAILABLE``), ``fake`` the in-memory one, ``cloud_run`` the job named by
    ``SSC_CELL_DEPLOYER_JOB`` (``projects/<p>/locations/<r>/jobs/<job>``)."""
    match env.get(DEPLOYER_ENV, ""):
        case "":
            return None
        case "fake":
            return FakeCellDeployer()
        case "cloud_run":
            job = env.get(DEPLOYER_JOB_ENV, "")
            parts = job.split("/")
            if tuple(parts[0::2]) != JOB_KEYS or not all(parts[1::2]):
                raise ValueError(f"{DEPLOYER_JOB_ENV} must be projects/<p>/locations/<r>/jobs/<j>")
            return CloudRunCellDeployer(job, MetadataAccessTokens())
        case other:
            raise ValueError(f"unknown {DEPLOYER_ENV} {other!r}")
