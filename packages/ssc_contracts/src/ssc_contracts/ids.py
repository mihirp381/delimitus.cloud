import secrets
from typing import Final, Literal, get_args

Prefix = Literal[
    "org",  # organisation (one customer)
    "usr",  # user
    "grp",  # group
    "idl",  # identity link (issuer, subject) -> user
    "app",  # app
    "env",  # environment
    "rel",  # release
    "dep",  # deployment
    "gnt",  # sharing grant
    "sec",  # secret reference
    "sch",  # schedule (also the subject of a schedule-principal identity note)
    "con",  # connection to a company database
    "cgr",  # connection grant (one environment's use of one connection)
    "apr",  # approval request
    "pol",  # policy decision
    "cell",  # customer cell
    "tmr",  # timer run
    "bdl",  # source bundle
    "bld",  # build
    "kil",  # kill-switch run
    "dcn",  # directory connection (WorkOS organisation, directory, SSO connections)
    "ses",  # auth-host sign-in session
    "lgc",  # one-time login code
    "rft",  # command-line refresh token
    "dvg",  # device authorisation grant
    "ulg",  # unlinked login
]
PREFIXES: Final[tuple[str, ...]] = get_args(Prefix)
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
