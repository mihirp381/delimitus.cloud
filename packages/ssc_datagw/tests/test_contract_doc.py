"""``docs/contracts/data-gateway.md`` names only error codes the gateway answers."""

import re
from pathlib import Path

from ssc_datagw.server import ERRORS

DOC = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "data-gateway.md"
CODE = re.compile(
    r"`([A-Z][A-Z0-9_]*(?:_REFUSED|_UNAVAILABLE|_STALE|_LIMIT|_TOO_LARGE|_FOUND|_ERROR"
    r"|_FAILED|_SPENT|_EXCEEDED|_SUSPENDED|_GRANTED|_ACTIVE))`"
)
VENDOR = frozenset({"INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND", "VIEW_NAME_NOT_FOUND"})
"""Airtable's own error types, which the Airtable connector's error table maps to ours."""


def test_every_error_code_the_doc_names_is_one_the_gateway_answers() -> None:
    named = set(CODE.findall(DOC.read_text()))
    assert {"CONNECTION_UNAVAILABLE", "QUERY_REFUSED", "BODY_TOO_LARGE"} <= named
    assert VENDOR <= named
    assert sorted(named - VENDOR - set(ERRORS)) == []
