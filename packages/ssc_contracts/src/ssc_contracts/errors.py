"""The error catalogue: every refusal the API can return, in RFC 9457 problem-detail form.

Three rules, mined from what went wrong in Delimitus (three renderers, evidence leaking into
user-facing text):

1. **One renderer.** Every problem body is produced by ``ssc_control.api.problems``. Nothing else
   builds a ``{"type": ..., "title": ...}`` document.
2. **Fixed text.** ``title`` and ``detail`` are complete sentences written here, with no
   placeholders. A client can show them to a person as they are, in any language layer later.
3. **Evidence stays out of the body.** SQLSTATEs, constraint names, key values, header values,
   stack traces and anything else that would help an attacker or embarrass a customer are
   logged server-side under the ``request_id`` that the body does carry. Support joins the two.

Adding a code means adding it to :class:`ErrorCode` and to :data:`CATALOGUE`; a test fails when
either side is missing, when a detail contains ``{``, or when a status is outside 400..599.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
PROBLEM_TYPE_BASE: Final = "https://delimitus.com/errors/"


class ErrorCode(StrEnum):
    # request shape
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    # credentials
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    # idempotency (every POST)
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    IDEMPOTENCY_IN_FLIGHT = "IDEMPOTENCY_IN_FLIGHT"
    # optimistic concurrency (sharing-rule edits)
    PRECONDITION_REQUIRED = "PRECONDITION_REQUIRED"
    PRECONDITION_STALE = "PRECONDITION_STALE"
    # state of the record
    ALREADY_EXISTS = "ALREADY_EXISTS"
    REFERENCE_NOT_FOUND = "REFERENCE_NOT_FOUND"
    DEPLOYMENT_IN_FLIGHT = "DEPLOYMENT_IN_FLIGHT"
    LAST_ORG_ADMIN = "LAST_ORG_ADMIN"
    OWNER_NOT_ACTIVE = "OWNER_NOT_ACTIVE"
    RECORD_IMMUTABLE = "RECORD_IMMUTABLE"
    SCHEDULE_DELETED = "SCHEDULE_DELETED"
    # approvals (SSC-045)
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_NOT_PENDING = "APPROVAL_NOT_PENDING"
    SELF_APPROVAL_REFUSED = "SELF_APPROVAL_REFUSED"
    AGENT_SESSION_REFUSED = "AGENT_SESSION_REFUSED"
    APPROVER_NOT_ELIGIBLE = "APPROVER_NOT_ELIGIBLE"
    # source upload (SSC-014)
    MANIFEST_INVALID = "MANIFEST_INVALID"
    BUNDLE_TOO_LARGE = "BUNDLE_TOO_LARGE"
    BUNDLE_MALFORMED = "BUNDLE_MALFORMED"
    SECRET_IN_BUNDLE = "SECRET_IN_BUNDLE"  # noqa: S105  (an error code, not a secret)
    BUNDLE_DIGEST_MISMATCH = "BUNDLE_DIGEST_MISMATCH"
    BUNDLE_NOT_UPLOADED = "BUNDLE_NOT_UPLOADED"
    UPLOAD_URL_INVALID = "UPLOAD_URL_INVALID"
    APP_NOT_ACTIVE = "APP_NOT_ACTIVE"
    # builds, releases and deployments (SSC-016)
    BUILD_IN_FLIGHT = "BUILD_IN_FLIGHT"
    RELEASE_ENVIRONMENT_MISMATCH = "RELEASE_ENVIRONMENT_MISMATCH"
    # kill switch (SSC-025)
    KILL_SWITCH_IN_FLIGHT = "KILL_SWITCH_IN_FLIGHT"
    APP_ALREADY_ACTIVE = "APP_ALREADY_ACTIVE"
    # timers (SSC-041)
    TIMER_RUN_IN_FLIGHT = "TIMER_RUN_IN_FLIGHT"
    SCHEDULE_CANNOT_RESUME = "SCHEDULE_CANNOT_RESUME"
    # ours
    INTERNAL = "INTERNAL"


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    status: int
    title: str
    detail: str


CATALOGUE: Final[dict[ErrorCode, CatalogueEntry]] = {
    ErrorCode.VALIDATION_FAILED: CatalogueEntry(
        422,
        "The request is not valid.",
        "One or more fields are missing or have the wrong shape. Correct the request and retry.",
    ),
    ErrorCode.NOT_FOUND: CatalogueEntry(
        404,
        "Not found.",
        "There is nothing at this address that you can see.",
    ),
    ErrorCode.METHOD_NOT_ALLOWED: CatalogueEntry(
        405,
        "This method is not allowed here.",
        "The address exists but does not accept this HTTP method.",
    ),
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: CatalogueEntry(
        415,
        "Unsupported request body.",
        "Send the request body as JSON with a matching Content-Type header.",
    ),
    ErrorCode.UNAUTHENTICATED: CatalogueEntry(
        401,
        "Sign in to continue.",
        "The request carried no usable credential. Sign in again and retry.",
    ),
    ErrorCode.FORBIDDEN: CatalogueEntry(
        403,
        "You cannot do this.",
        "Your credential is valid but does not allow this action.",
    ),
    ErrorCode.RATE_LIMITED: CatalogueEntry(
        429,
        "Too many requests.",
        "This credential has sent requests faster than allowed. Wait and retry.",
    ),
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED: CatalogueEntry(
        400,
        "An Idempotency-Key header is required.",
        "Every POST needs an Idempotency-Key header so a retry can be told from a second request.",
    ),
    ErrorCode.IDEMPOTENCY_KEY_REUSED: CatalogueEntry(
        422,
        "This Idempotency-Key was used for a different request.",
        "Use a new key for a new request. A retry must repeat the original request exactly.",
    ),
    ErrorCode.IDEMPOTENCY_IN_FLIGHT: CatalogueEntry(
        409,
        "This request is already being processed.",
        "A request with the same Idempotency-Key has not finished. "
        "Wait and retry with the same key.",
    ),
    ErrorCode.PRECONDITION_REQUIRED: CatalogueEntry(
        428,
        "An If-Match header is required.",
        "Read the current version first and send it back in If-Match, "
        "so two edits cannot overwrite each other.",
    ),
    ErrorCode.PRECONDITION_STALE: CatalogueEntry(
        412,
        "Someone else changed this first.",
        "The version in If-Match is no longer current. Read again, review the change, and retry.",
    ),
    ErrorCode.ALREADY_EXISTS: CatalogueEntry(
        409,
        "Something with this name already exists.",
        "Choose a different name, or use the existing record.",
    ),
    ErrorCode.REFERENCE_NOT_FOUND: CatalogueEntry(
        422,
        "The request points at something that does not exist.",
        "One of the ids in the request does not name a record you can see.",
    ),
    ErrorCode.DEPLOYMENT_IN_FLIGHT: CatalogueEntry(
        409,
        "A deployment is already in progress.",
        "Only one deployment per environment can run at a time. "
        "Wait for it to finish, or roll back.",
    ),
    ErrorCode.LAST_ORG_ADMIN: CatalogueEntry(
        409,
        "An organisation needs at least one active admin.",
        "Make another person an admin before removing this one.",
    ),
    ErrorCode.OWNER_NOT_ACTIVE: CatalogueEntry(
        422,
        "The owner must be an active member.",
        "An app can only be owned by an active member of the organisation.",
    ),
    ErrorCode.RECORD_IMMUTABLE: CatalogueEntry(
        409,
        "This record cannot be changed.",
        "Releases and audit entries are written once. Create a new one instead.",
    ),
    ErrorCode.SCHEDULE_DELETED: CatalogueEntry(
        409,
        "This schedule was deleted.",
        "A deleted schedule cannot be changed. Create a new schedule instead.",
    ),
    ErrorCode.APPROVAL_REQUIRED: CatalogueEntry(
        409,
        "This change needs an approval first.",
        "Another admin of the organisation must approve exactly this change before it is applied. "
        "Ask for an approval and retry once it is approved.",
    ),
    ErrorCode.APPROVAL_NOT_PENDING: CatalogueEntry(
        409,
        "This approval request is already decided.",
        "Only a pending request can be decided. Read the request to see its decision.",
    ),
    ErrorCode.SELF_APPROVAL_REFUSED: CatalogueEntry(
        403,
        "You cannot decide your own request.",
        "An approval must be decided by a different person from the one who asked for it.",
    ),
    ErrorCode.AGENT_SESSION_REFUSED: CatalogueEntry(
        403,
        "An agent session cannot decide approvals.",
        "Approvals are decided by a person in an interactive session, never through an agent.",
    ),
    ErrorCode.APPROVER_NOT_ELIGIBLE: CatalogueEntry(
        403,
        "This person cannot decide this request.",
        "The approver must be an active admin of the organisation.",
    ),
    ErrorCode.MANIFEST_INVALID: CatalogueEntry(
        422,
        "The bundle's ssc.toml is not valid.",
        "The manifest does not follow ssc/v1. Run ssc doctor to see each problem with its line, "
        "fix them and deploy again.",
    ),
    ErrorCode.BUNDLE_TOO_LARGE: CatalogueEntry(
        413,
        "The bundle is too large.",
        "The bundle is over the compressed size, unpacked size or file count limit. Leave "
        "dependencies and generated files out with .sscignore and retry.",
    ),
    ErrorCode.BUNDLE_MALFORMED: CatalogueEntry(
        422,
        "The bundle is not a valid source bundle.",
        "A bundle is a gzip-compressed tar of regular files and folders with safe relative names "
        "and no .env files. Pack it with ssc deploy and retry.",
    ),
    ErrorCode.SECRET_IN_BUNDLE: CatalogueEntry(
        422,
        "The bundle contains a secret.",
        "A value that looks like a credential was found in the source. Remove it from the code, "
        "store it as an app secret and deploy again.",
    ),
    ErrorCode.BUNDLE_DIGEST_MISMATCH: CatalogueEntry(
        422,
        "The uploaded bytes do not match the bundle.",
        "The size or sha256 of the uploaded object differs from the one declared. Upload the same "
        "bytes again.",
    ),
    ErrorCode.BUNDLE_NOT_UPLOADED: CatalogueEntry(
        409,
        "The bundle has not been uploaded.",
        "Upload the bundle to its upload URL before completing it.",
    ),
    ErrorCode.UPLOAD_URL_INVALID: CatalogueEntry(
        403,
        "This upload URL is not valid.",
        "The URL has expired, was altered, or is for a different method. Ask for a new URL and "
        "retry.",
    ),
    ErrorCode.APP_NOT_ACTIVE: CatalogueEntry(
        409,
        "This app is not active.",
        "A disabled or quarantined app takes no new source, a quarantined app takes no sharing "
        "changes, and an app is stopped once per mode. Enable the app first.",
    ),
    ErrorCode.BUILD_IN_FLIGHT: CatalogueEntry(
        409,
        "This bundle is already being built here.",
        "A build of the same bundle for this environment is queued or running. Poll it instead "
        "of starting another.",
    ),
    ErrorCode.RELEASE_ENVIRONMENT_MISMATCH: CatalogueEntry(
        409,
        "This release was built for another environment.",
        "Each environment runs releases built for it. Deploy a release built for this "
        "environment, or build the same bundle for it first.",
    ),
    ErrorCode.KILL_SWITCH_IN_FLIGHT: CatalogueEntry(
        409,
        "The kill switch is already running for this app.",
        "One kill switch runs per app at a time. Wait for it to finish, then retry.",
    ),
    ErrorCode.APP_ALREADY_ACTIVE: CatalogueEntry(
        409,
        "This app is already active.",
        "Only a disabled or quarantined app can be enabled.",
    ),
    ErrorCode.TIMER_RUN_IN_FLIGHT: CatalogueEntry(
        409,
        "This schedule already has a run in progress.",
        "A manual run is waiting or a run is running. Wait for it to finish, then retry.",
    ),
    ErrorCode.SCHEDULE_CANNOT_RESUME: CatalogueEntry(
        409,
        "This schedule cannot be resumed yet.",
        "It was paused because the app is disabled or quarantined, because it is in preview, or "
        "because the app's owner or the person who deployed it may no longer run it. Enable the "
        "app, restore that access, or deploy again.",
    ),
    ErrorCode.INTERNAL: CatalogueEntry(
        500,
        "Something went wrong on our side.",
        "The request was not completed. "
        "Retry later and quote the request id if you contact support.",
    ),
}


def problem_type(code: ErrorCode) -> str:
    return PROBLEM_TYPE_BASE + code.value


class Problem(BaseModel):
    """The wire shape of every refusal (RFC 9457). Rendered by exactly one function."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str = Field(description="A URL naming the error code.")
    title: str = Field(description="Fixed, human-readable summary of the code.")
    status: int = Field(ge=400, le=599)
    detail: str = Field(description="Fixed, human-readable explanation. Never carries evidence.")
    instance: str = Field(description="The request path.")
    code: ErrorCode
    request_id: str = Field(description="Quote this to support. Evidence is logged under it.")


ProblemMember = Literal["type", "title", "status", "detail", "instance", "code", "request_id"]
PROBLEM_MEMBERS: Final[tuple[ProblemMember, ...]] = (
    "type",
    "title",
    "status",
    "detail",
    "instance",
    "code",
    "request_id",
)
