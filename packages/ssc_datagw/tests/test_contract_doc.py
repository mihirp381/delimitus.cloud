"""``docs/contracts/data-gateway.md`` names only error codes the gateway answers, and it and the
gateway's sources, which customers read, name none of our own accounts, cells or harnesses."""

import re
from pathlib import Path

from ssc_datagw.server import ERRORS

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "contracts" / "data-gateway.md"
SOURCES = ROOT / "packages" / "ssc_datagw" / "src" / "ssc_datagw"
INTERNAL = re.compile(
    r"founder|proof cell|proofrun|our GCP sandbox|BigQuery sandbox|sandbox sources"
    r"|trial account on 20",
    re.IGNORECASE,
)
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


def test_customer_facing_text_names_none_of_our_own_accounts_cells_or_harnesses() -> None:
    texts = [DOC, *sorted(SOURCES.glob("*.py")), *sorted(SOURCES.glob("*.sql"))]
    found = {
        f"{path.relative_to(ROOT)}:{n}": m.group(0)
        for path in texts
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if (m := INTERNAL.search(line))
    }
    assert found == {}
