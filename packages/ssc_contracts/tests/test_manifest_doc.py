"""``docs/contracts/manifest.md`` names every field of the model it freezes."""

import re
from pathlib import Path
from typing import Any

from ssc_contracts.manifest import Manifest, default_manifest, load_manifest
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


def test_the_doc_example_keeps_its_digest() -> None:
    example = re.search(r"## Example\n\n```toml\n(.*?)```", DOC.read_text(), re.DOTALL)
    assert example is not None
    assert manifest_digest(load_manifest(example.group(1))) == (
        "sha256:e12d0ecb634a12d1d4da449f3ee62c9f6ec9ccfa65d3fc9c5a3f9fad0c45a373"
    )


def test_the_doc_settles_sessions_sleep_and_billing() -> None:
    text = DOC.read_text()
    assert "Pending changes" not in text
    for name in ("Streamlit", "Gradio", "Dash", "Shiny", "60 minutes", "STATE_SQLITE_EPHEMERAL"):
        assert name in text, name
    assert "first request after a quiet spell is slow" in text
    assert [line for line in text.splitlines() if "billing" in line] == [
        line for line in text.splitlines() if line.startswith("There is no `billing` key.")
    ]
