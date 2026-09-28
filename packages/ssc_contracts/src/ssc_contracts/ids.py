import secrets
from typing import Final, Literal

Prefix = Literal["usr", "sch", "app", "rel", "env", "cell", "org", "grp", "con", "tmr"]
_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz0123456789"
ID_LENGTH: Final = 20


def new_id(prefix: Prefix) -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(ID_LENGTH))
    return f"{prefix}_{body}"


def prefix_of(value: str) -> str:
    head, sep, body = value.partition("_")
    if not sep or len(body) != ID_LENGTH or not body.isalnum() or not body.islower():
        raise ValueError(f"not an SSC id: {value!r}")
    return head
