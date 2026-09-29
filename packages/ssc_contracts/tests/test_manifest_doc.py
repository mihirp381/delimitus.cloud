"""``docs/contracts/manifest.md`` names every field of the model it freezes."""

from pathlib import Path
from typing import Any

from ssc_contracts.manifest import Manifest, default_manifest
from ssc_shared.canonical import manifest_digest

DOC = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "manifest.md"


def properties(schema: dict[str, Any]) -> set[str]:
    names = set(schema.get("properties", {}))
    for sub in schema.get("$defs", {}).values():
        names |= properties(sub)
    return names


def test_every_schema_field_is_named_in_the_doc() -> None:
    text = DOC.read_text()
    names = properties(Manifest.model_json_schema())
    assert "timeout_seconds" in names and "class" in names
    forms = ("`{}`", "`[{}]`", "`[[{}]]`")
    missing = sorted(n for n in names if not any(f.format(n) in text for f in forms))
    assert missing == []


def test_the_doc_states_the_default_digest() -> None:
    assert manifest_digest(default_manifest()) in DOC.read_text()
