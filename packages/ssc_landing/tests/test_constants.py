"""M-11: the limits CLAIMS.md cites are read from it and compared with the code's constants."""

import re
from pathlib import Path

from ssc_agent.cloud_run import DEFAULT_TIMEOUT_SECONDS
from ssc_contracts.app_database import BASE_TIER_MAX_CONNECTIONS, SUPERUSER_RESERVE, ceiling
from ssc_datagw.files import LINK_SECONDS

REGISTER = (Path(__file__).resolve().parents[3] / "landing" / "CLAIMS.md").read_text(
    encoding="utf-8"
)
ORDINALS: tuple[str, ...] = tuple(
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
    "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth".split()
)


def _row(register: str, claim: str) -> str:
    row = re.search(rf"^\| `{claim}` \|.*$", register, flags=re.M)
    assert row is not None, claim
    return row[0]


def cited(register: str) -> dict[str, int]:
    db = re.search(r"the (\w+) database refused `DB_TIER_FULL`", _row(register, "db"))
    link = re.search(r"get by a (\d+)-minute link", _row(register, "files"))
    refused = re.search(r"link refused after (\d+) s", _row(register, "files"))
    cut = re.search(r"a run longer than (\d+) s is cut", _row(register, "timers"))
    assert db and link and refused and cut
    return {
        "databases": ORDINALS.index(db[1]),
        "link_minutes": int(link[1]),
        "link_seconds": int(refused[1]),
        "request_seconds": int(cut[1]),
    }


def test_the_cited_limits_match_the_code() -> None:
    assert cited(REGISTER) == {
        "databases": ceiling(BASE_TIER_MAX_CONNECTIONS, SUPERUSER_RESERVE),
        "link_minutes": LINK_SECONDS // 60,
        "link_seconds": LINK_SECONDS,
        "request_seconds": DEFAULT_TIMEOUT_SECONDS,
    }


def test_a_changed_figure_is_read_as_changed() -> None:
    edited = REGISTER.replace("the eleventh database", "the twelfth database").replace(
        "link refused after 600 s", "link refused after 900 s"
    )
    assert cited(edited)["databases"] == 11
    assert cited(edited)["link_seconds"] == 900
