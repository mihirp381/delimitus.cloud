"""Polling builds and deployments until they end.

A build or deployment that ends badly becomes a :class:`CliError` whose code is the API's
``failure_code``, with ``status: null`` (nothing was refused) and ``instance`` naming the build or
operation. Time is counted in the sleeps between polls, so a test's no-op sleep still ends. One
:class:`Budget` is shared by every wait of a command, so ``--timeout`` bounds the whole command.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from ssc_cli.api import ApiClient, Sleep
from ssc_cli.errors import (
    BAD_RESPONSE,
    BUILD_FAILED,
    DEPLOYMENT_FAILED,
    DEPLOYMENT_SUPERSEDED,
    WAIT_TIMED_OUT,
    CliError,
    ErrorBody,
    ExitCode,
    local_error,
)
from ssc_cli.models import OperationOut
from ssc_contracts.build import FIX_ITS

POLL_SECONDS: Final = 2.0
DEFAULT_TIMEOUT: Final = 1800

# The next step for failure codes whose cause is known. Codes come from the API (decision 014).
FIXES: Final = {
    "APPROVAL_REQUIRED": "Production needs approval first. An org admin other than you decides "
    "the open approval requests for this app; then run the same command again.",
    "HEALTH_CHECK_FAILED": "The app did not answer on its health path in time. It must listen "
    "on 0.0.0.0 and the port in $PORT, and `health_path` under [runtime] in ssc.toml must answer. "
    "Run `ssc doctor`.",
    "MANIFEST_INVALID": "Run `ssc doctor` and fix each MANIFEST_INVALID finding.",
    "BUILD_TIMED_OUT": "The build ran for over 20 minutes. Run `ssc doctor`, then deploy again.",
    "APP_NOT_ACTIVE": "The app is not active, so it takes no deployments. `ssc status` shows its "
    "status; an org admin can tell you why.",
    "CELL_RESOURCE_FAILED": "Your company's database could not be created. Deploy again to retry; "
    "if it fails again, tell an org admin.",
    "DB_TIER_FULL": "Your company's database (db-f1-micro) holds ten app environments, previews "
    "included, and all are taken; nothing was created. An org admin can move to the bigger "
    "database (db-g1-small, about $26 a month), or remove an environment that has a database.",
    "DATABASE_UNAVAILABLE": "The app's database could not be reached from the platform. Deploy "
    "again to retry; if it fails again, tell an org admin.",
    "SNAPSHOT_UNCONFIRMED": "The platform did not confirm the new version in time. Nothing "
    "changed and the old version is still serving. Deploy again.",
    **FIX_ITS,
}


@dataclass(slots=True)
class Budget:
    """The seconds one command may wait in all, and those its sleeps have spent."""

    seconds: float
    spent: float = 0.0

    def sleep(self, sleep: Sleep) -> None:
        sleep(POLL_SECONDS)
        self.spent += POLL_SECONDS

    @property
    def used_up(self) -> bool:
        return self.spent >= self.seconds


def ended_badly(what: str, code: str, detail: str, instance: str) -> CliError:
    body = ErrorBody(
        code=code, title=f"The {what} failed.", detail=detail, status=None, instance=instance
    )
    return CliError(body, ExitCode.FAILED, fix=FIXES.get(code))


def _timed_out(what: str, state: str, budget: Budget, instance: str, next_step: str) -> CliError:
    body = ErrorBody(
        code=WAIT_TIMED_OUT,
        title=f"Stopped waiting for the {what}.",
        detail=f"It was still {state} after {budget.seconds:g} seconds and carries on without "
        f"ssc. {next_step}",
        status=None,
        instance=instance,
    )
    return CliError(body, ExitCode.FAILED)


def wait_for_build(
    client: ApiClient, build_id: str, *, sleep: Sleep, budget: Budget, next_step: str
) -> tuple[str, int]:
    """The id and number of the release the build made."""
    instance = f"/v1/builds/{build_id}"
    while True:
        build = client.get_build(build_id)
        if build.state == "succeeded":
            if build.release_id is None or build.release_number is None:
                raise local_error(
                    BAD_RESPONSE,
                    "The API answered in a shape this ssc does not understand.",
                    f"Build {build_id} succeeded without a release.",
                )
            return build.release_id, build.release_number
        if build.state == "failed":
            code = build.failure_code or BUILD_FAILED
            raise ended_badly("build", code, f"Build {build_id} failed with {code}.", instance)
        if budget.used_up:
            raise _timed_out("build", build.state, budget, instance, next_step)
        budget.sleep(sleep)


def wait_for_operation(  # noqa: PLR0913  (keyword-only)
    client: ApiClient,
    operation_id: str,
    *,
    sleep: Sleep,
    budget: Budget,
    next_step: str,
    note: Callable[[str], None] | None = None,
    told: str | None = None,
) -> OperationOut:
    """The deployment once it is ``healthy``. Each new ``notice`` (a deployment waiting for the
    company's database, SSC-087) goes to ``note`` once; ``told`` is one already given."""
    instance = f"/v1/operations/{operation_id}"
    while True:
        op = client.get_operation(operation_id)
        if note is not None and op.notice is not None and op.notice != told:
            note(op.notice)
            told = op.notice
        if op.state == "healthy":
            return op
        if op.state == "failed":
            code = op.failure_code or DEPLOYMENT_FAILED
            detail = f"Deployment {operation_id} failed with {code}; the environment is unchanged."
            raise ended_badly("deployment", code, detail, instance)
        if op.state == "superseded":
            detail = (
                f"Deployment {operation_id} was superseded by a newer deployment, which is "
                "what the environment runs or will run."
            )
            raise ended_badly("deployment", DEPLOYMENT_SUPERSEDED, detail, instance)
        if budget.used_up:
            raise _timed_out("deployment", op.state, budget, instance, next_step)
        budget.sleep(sleep)
