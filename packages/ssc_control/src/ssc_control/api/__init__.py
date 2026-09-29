"""The one HTTP API (SSC-011): ``/v1`` for people and their tools, ``/internal/v1`` for cells.

Conventions, each enforced by code in this package and by ``tests/test_api.py``:

* every refusal is an RFC 9457 problem from the catalogue in ``ssc_contracts.errors``, rendered by
  the single function in ``problems.py``; evidence is logged under the request id, never sent;
* every POST carries ``Idempotency-Key``; the claim is a row written in the request's own
  transaction (``idempotency.py``), so a retry replays the first answer and a refusal leaves no
  trace;
* every edit to sharing rules carries ``If-Match`` with the environment's ``grants_version``;
* a deploy is a long-running operation: 202 now, a status endpoint later;
* one transaction per request (``uow.py``), bound to the credential's org, that also holds the
  audit row (``ssc_control.audit``);
* per-credential rate limits (``ratelimit.py``);
* the OpenAPI file is committed at ``docs/api/openapi.json`` and a breaking change fails CI.
"""

from ssc_control.api.app import create_app
from ssc_control.api.settings import Settings

__all__ = ["Settings", "create_app"]
