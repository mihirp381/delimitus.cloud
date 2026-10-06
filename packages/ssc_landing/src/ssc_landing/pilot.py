"""A pilot request: what the form sends, checked, or a refusal that says which fields to fix.

The refusal cases follow the Delimitus waitlist (``waitlist_store.py``): a request is read by a
person, so it needs a name, a company and an address that can be answered.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import parse_qs

REQUIRED: Final = ("firstName", "lastName", "email", "company")
OPTIONAL: Final = ("tools",)
TRAP: Final = "website"
"""The honeypot: hidden from people, filled in by form-filling bots."""
MAX_FIELD: Final = 200
_EMAIL: Final = re.compile(r"[^\s@,<>\"]+@[^\s@,<>\"]+\.[^\s@,<>\"]+")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class PilotRequest:
    first_name: str
    last_name: str
    email: str
    company: str
    tools: str
    asked_at: datetime

    def to_json(self) -> bytes:
        """One stored object. These fields are on the trust pack's PII list."""
        return json.dumps(
            {
                "schema": "ssc-pilot-request/v1",
                "firstName": self.first_name,
                "lastName": self.last_name,
                "email": self.email,
                "company": self.company,
                "tools": self.tools,
                "askedAt": self.asked_at.isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")


class PilotRefused(Exception):  # noqa: N818  (a refusal, not a fault)
    """The request is not stored. ``message`` is shown to the visitor as it is."""

    def __init__(self, status: int, code: str, message: str, fields: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields


def parse_body(content_type: str, body: bytes) -> dict[str, str]:
    """The submitted fields, from the page's script (JSON) or a plain form post."""
    kind = content_type.split(";", 1)[0].strip().lower()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise PilotRefused(400, "UNREADABLE", "The request could not be read.") from None
    if kind == "application/json":
        try:
            raw: object = json.loads(text or "{}")
        except json.JSONDecodeError:
            raise PilotRefused(400, "UNREADABLE", "The request could not be read.") from None
        if not isinstance(raw, Mapping):
            raise PilotRefused(400, "UNREADABLE", "The request could not be read.")
        items: Mapping[object, object] = raw  # pyright: ignore[reportUnknownVariableType]
        return {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, str)}
    if kind == "application/x-www-form-urlencoded":
        return {k: v[0] for k, v in parse_qs(text, keep_blank_values=True).items()}
    raise PilotRefused(415, "UNSUPPORTED", "Send the form from the page.")


def is_trapped(fields: Mapping[str, str]) -> bool:
    return fields.get(TRAP, "").strip() != ""


def pilot_request_from(fields: Mapping[str, str], asked_at: datetime) -> PilotRequest:
    """The checked request, or :class:`PilotRefused` naming every field to fix."""
    values = {name: fields.get(name, "").strip() for name in (*REQUIRED, *OPTIONAL)}
    values["email"] = values["email"].lower()
    bad = [n for n in REQUIRED if not values[n]]
    if values["email"] and not _EMAIL.fullmatch(values["email"]):
        bad.append("email")
    bad += [n for n, v in values.items() if len(v) > MAX_FIELD or _CONTROL.search(v)]
    if bad:
        named = tuple(dict.fromkeys(bad))
        raise PilotRefused(
            422,
            "FIELDS_INVALID",
            "Check the highlighted fields: a name, a company and a work email we can reply to.",
            named,
        )
    return PilotRequest(
        first_name=values["firstName"],
        last_name=values["lastName"],
        email=values["email"],
        company=values["company"],
        tools=values["tools"],
        asked_at=asked_at,
    )
