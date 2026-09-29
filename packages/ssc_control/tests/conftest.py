"""Fixtures the control-plane tests share. One migrated postgres:18 serves the whole session;
tests create their own orgs, so they never see each other's rows."""

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
