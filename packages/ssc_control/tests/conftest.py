"""Fixtures the control-plane tests share. One migrated postgres:18 serves the whole session;
tests create their own orgs, so they never see each other's rows."""

import logging
from collections.abc import Iterator

import pytest
from ssc_testkit import Dsns, SigningKey, control_db, new_signing_key


@pytest.fixture(scope="session")
def dsns() -> Iterator[Dsns]:
    with control_db() as d:
        yield d


@pytest.fixture(scope="session")
def signing_key() -> SigningKey:
    return new_signing_key()


@pytest.fixture
def unredacted_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Logging as a fresh process has it (no redaction yet), put back afterwards. The database
    DSN is unset, so an entrypoint's ``main`` stops before connecting."""
    monkeypatch.delenv("SSC_DATABASE_DSN", raising=False)
    root = logging.getLogger()
    factory, handlers = logging.getLogRecordFactory(), list(root.handlers)
    filters = {h: list(h.filters) for h in handlers}
    logging.setLogRecordFactory(logging.LogRecord)
    yield
    logging.setLogRecordFactory(factory)
    root.handlers[:] = handlers
    for h, f in filters.items():
        h.filters[:] = f
