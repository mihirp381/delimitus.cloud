import re
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
    "ntf",  # notification outbox row
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
    "oac",  # OAuth authorization code
    "onb",  # onboarding of one customer
]
PREFIXES: Final[tuple[str, ...]] = get_args(Prefix)
_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz0123456789"
ID_LENGTH: Final = 20
_ID: Final = re.compile(rf"([a-z]{{3,4}})_([a-z0-9]{{{ID_LENGTH}}})")


def new_id(prefix: Prefix) -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(ID_LENGTH))
    return f"{prefix}_{body}"


def prefix_of(value: str) -> str:
    match = _ID.fullmatch(value)
    if match is None or match[1] not in PREFIXES:
        raise ValueError(f"not an SSC id: {value!r}")
    return match[1]
