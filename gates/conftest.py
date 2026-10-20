"""``pytest gates`` runs the gate tests here, never the planted failures under ``fixtures/``,
which ``run_gates.py`` runs one by one and expects to fail."""

from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def pytest_ignore_collect(collection_path: Path) -> bool | None:
    return True if collection_path == FIXTURES else None
