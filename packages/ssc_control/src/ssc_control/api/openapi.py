"""The OpenAPI document, rendered deterministically so it can be committed and diffed.

``docs/api/openapi.json`` is the committed copy. ``tools/openapi_check.py`` fails when it drifts
from this rendering; ``tools/openapi_breaking.py`` fails when a change breaks clients.
"""

import json
from typing import Any

from ssc_control.api.app import create_app
from ssc_control.api.settings import Settings


def build_spec() -> dict[str, Any]:
    return create_app(Settings.for_spec()).openapi()


def spec_json() -> str:
    return json.dumps(build_spec(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
