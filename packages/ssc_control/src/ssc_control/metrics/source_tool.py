"""Which builder tool a change came from, for the source tool mix.

An agent credential's ``client_id`` wins; otherwise the ``X-SSC-Source-Tool`` header a client
sends (a cross-lane contract, recorded in ``docs/api/README.md``). The value is lowercased and
must match ``[a-z0-9._-]{1,40}``; anything else is recorded as ``other``. Neither present: None.
"""

import re
from typing import Final, Protocol

SOURCE_TOOL_HEADER: Final = "X-SSC-Source-Tool"
OTHER: Final = "other"
_SHAPE: Final = re.compile(r"[a-z0-9._-]{1,40}")


class _Caller(Protocol):
    @property
    def is_agent(self) -> bool: ...

    @property
    def client_id(self) -> str | None: ...


def normalise(value: str | None) -> str | None:
    """A declared tool name in its stored shape; None when nothing was declared."""
    if value is None or not value.strip():
        return None
    name = value.strip().lower()
    return name if _SHAPE.fullmatch(name) else OTHER


def source_tool_of(caller: _Caller, header: str | None) -> str | None:
    """The agent's ``client_id`` when the credential is an agent's, else the header."""
    if caller.is_agent and caller.client_id:
        return normalise(caller.client_id)
    return normalise(header)
