"""Pieces every ``/v1`` resource module shares: strict models, id and slug types, the user check
and the ``If-Match``/``ETag`` helpers."""

import re
from typing import Annotated, Final

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field

from ssc_contracts.errors import ErrorCode
from ssc_control.api.auth import PrincipalKind
from ssc_control.api.problems import Refusal
from ssc_control.api.uow import UnitOfWork

IF_MATCH: Final = "If-Match"
ETAG: Final = "ETag"
_ETAG_RE: Final = re.compile(r'^(?:W/)?"?(\d{1,18})"?$')

Id = Annotated[str, Path(pattern=r"^[a-z]{3}_[a-z0-9]{20}$")]
Slug = Annotated[
    str,
    Field(
        pattern=r"^[a-z]([a-z0-9-]{0,38}[a-z0-9])?$",
        description="Host label of the app. Lower-case, no leading digit, no `--`.",
    ),
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def require_user(uow: UnitOfWork) -> str:
    """The caller's user id; any other credential kind is ``FORBIDDEN``."""
    if uow.principal.kind is not PrincipalKind.USER:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"kind": uow.principal.kind.value})
    return uow.principal.subject


def etag(version: int) -> str:
    return f'"{version}"'


def parse_if_match(value: str | None) -> int:
    if value is None:
        raise Refusal(ErrorCode.PRECONDITION_REQUIRED)
    m = _ETAG_RE.match(value.strip())
    if m is None:
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"if_match": value})
    return int(m.group(1))
