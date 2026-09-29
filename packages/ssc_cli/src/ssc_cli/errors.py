"""Exit codes, and the one error type every command failure becomes.

Server refusals keep the problem members the API sent. Failures found on this machine use the
codes below and carry ``status: null``, so a script can tell the two apart.
"""

from enum import IntEnum
from typing import Final

from pydantic import BaseModel, ConfigDict


class ExitCode(IntEnum):
    OK = 0
    FAILED = 1
    USAGE = 2
    AUTH = 3
    BLOCKED = 4
    NETWORK = 5


# Codes for failures found locally. Server codes come from the API's error catalogue.
NO_TOKEN: Final = "NO_TOKEN"  # noqa: S105  (an error code, not a secret)
NO_KEYCHAIN: Final = "NO_KEYCHAIN"
BAD_TOKEN_INPUT: Final = "BAD_TOKEN_INPUT"  # noqa: S105  (an error code, not a secret)
BAD_API_URL: Final = "BAD_API_URL"
BAD_CONFIG: Final = "BAD_CONFIG"
NETWORK_ERROR: Final = "NETWORK_ERROR"
BAD_RESPONSE: Final = "BAD_RESPONSE"
APP_NOT_FOUND: Final = "APP_NOT_FOUND"
ENVIRONMENT_NOT_FOUND: Final = "ENVIRONMENT_NOT_FOUND"
LOCAL_CODES: Final = frozenset(
    {
        NO_TOKEN,
        NO_KEYCHAIN,
        BAD_TOKEN_INPUT,
        BAD_API_URL,
        BAD_CONFIG,
        NETWORK_ERROR,
        BAD_RESPONSE,
        APP_NOT_FOUND,
        ENVIRONMENT_NOT_FOUND,
    }
)


class ErrorBody(BaseModel):
    """The ``error`` member printed under ``--json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    title: str
    detail: str
    status: int | None = None
    request_id: str | None = None
    instance: str | None = None
    type: str | None = None


class CliError(Exception):
    """A failure to report to the person and turn into an exit code."""

    def __init__(self, body: ErrorBody, exit_code: ExitCode, fix: str | None = None) -> None:
        super().__init__(body.code)
        self.body = body
        self.exit_code = exit_code
        # The next step, printed as a ``Fix:`` line without ``--json``. Not part of the JSON.
        self.fix = fix


def local_error(
    code: str, title: str, detail: str, exit_code: ExitCode = ExitCode.FAILED
) -> CliError:
    return CliError(ErrorBody(code=code, title=title, detail=detail), exit_code)


def api_error(body: ErrorBody) -> CliError:
    """A refusal from the API. A 401 exits 3 like a missing token; everything else exits 1."""
    return CliError(body, ExitCode.AUTH if body.status == 401 else ExitCode.FAILED)
