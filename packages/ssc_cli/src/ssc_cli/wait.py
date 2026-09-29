"""Polling builds and deployments until they end.

A build or deployment that ends badly becomes a :class:`CliError` whose code is the API's
``failure_code``, with ``status: null`` (nothing was refused) and ``instance`` naming the build or
operation. Time is counted in the sleeps between polls, so a test's no-op sleep still ends.
"""

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
}


def ended_badly(what: str, code: str, detail: str, instance: str) -> CliError:
    body = ErrorBody(
        code=code, title=f"The {what} failed.", detail=detail, status=None, instance=instance
    )
    return CliError(body, ExitCode.FAILED, fix=FIXES.get(code))


def _timed_out(what: str, state: str, timeout: float, instance: str, follow: str) -> CliError:
    body = ErrorBody(
        code=WAIT_TIMED_OUT,
        title=f"Stopped waiting for the {what}.",
        detail=f"It was still {state} after {timeout:g} seconds and carries on without ssc. "
        f"Follow it with `{follow}`.",
        status=None,
        instance=instance,
    )
    return CliError(body, ExitCode.FAILED)


def wait_for_build(
    client: ApiClient, build_id: str, *, sleep: Sleep, timeout: float, follow: str
) -> tuple[str, int]:
    """The id and number of the release the build made."""
    instance = f"/v1/builds/{build_id}"
    waited = 0.0
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
        if waited >= timeout:
            raise _timed_out("build", build.state, timeout, instance, follow)
        sleep(POLL_SECONDS)
        waited += POLL_SECONDS


def wait_for_operation(
    client: ApiClient, operation_id: str, *, sleep: Sleep, timeout: float, follow: str
) -> OperationOut:
    """The deployment once it is ``healthy``."""
    instance = f"/v1/operations/{operation_id}"
    waited = 0.0
    while True:
        op = client.get_operation(operation_id)
        if op.state == "healthy":
            return op
        if op.state == "failed":
            code = op.failure_code or DEPLOYMENT_FAILED
            detail = f"Deployment {operation_id} failed with {code}; the environment is unchanged."
            raise ended_badly("deployment", code, detail, instance)
        if op.state == "superseded":
            detail = f"Deployment {operation_id} was superseded by a newer deployment, which is "
            "what the environment runs or will run."
            raise ended_badly("deployment", DEPLOYMENT_SUPERSEDED, detail, instance)
        if waited >= timeout:
            raise _timed_out("deployment", op.state, timeout, instance, follow)
        sleep(POLL_SECONDS)
        waited += POLL_SECONDS
