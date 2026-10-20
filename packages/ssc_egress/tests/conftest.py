import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def unredacted_logging() -> Iterator[None]:
    """Logging as a fresh process has it (no redaction yet), put back afterwards."""
    root = logging.getLogger()
    factory, handlers = logging.getLogRecordFactory(), list(root.handlers)
    filters = {h: list(h.filters) for h in handlers}
    logging.setLogRecordFactory(logging.LogRecord)
    yield
    logging.setLogRecordFactory(factory)
    root.handlers[:] = handlers
    for h, f in filters.items():
        h.filters[:] = f
