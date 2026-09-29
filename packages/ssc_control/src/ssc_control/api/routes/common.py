"""Shared response declarations so the OpenAPI file documents every refusal as a problem."""

from typing import Any, Final

from ssc_contracts.errors import CATALOGUE, PROBLEM_MEDIA_TYPE, ErrorCode

PROBLEM_REF: Final = "#/components/schemas/Problem"


def problem_responses(*codes: ErrorCode) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` entries for the given codes, grouped by status.

    The schema is a reference; ``app.py`` adds the ``Problem`` component to the document.
    """
    out: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        entry = CATALOGUE[code]
        existing = out.get(entry.status)
        names = (
            f"`{code.value}`" if existing is None else f"{existing['description']}, `{code.value}`"
        )
        out[entry.status] = {
            "description": names,
            "content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": PROBLEM_REF}}},
        }
    return out


AUTHENTICATED: Final = problem_responses(
    ErrorCode.UNAUTHENTICATED, ErrorCode.RATE_LIMITED, ErrorCode.VALIDATION_FAILED
)
POST_COMMON: Final = problem_responses(
    ErrorCode.UNAUTHENTICATED,
    ErrorCode.RATE_LIMITED,
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
    ErrorCode.IDEMPOTENCY_KEY_REUSED,
    ErrorCode.IDEMPOTENCY_IN_FLIGHT,
    ErrorCode.VALIDATION_FAILED,
)
