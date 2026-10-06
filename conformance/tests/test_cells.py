"""SSC_NIGHT_CELLS is read in one place, and a mistake in it says which cell and which key."""

import copy
import json
from typing import Any

import pytest

from ssc_conformance import cells

CELL_ONE: dict[str, Any] = {
    "project": "ssc-c-one",
    "label": "cell one",
    "agent_url": "https://agent-one.example.test",
    "app_url": "https://app-one.example.test",
    "gateway_url": "https://gateway-one-uc.a.run.app",
    "range": "10.20.0.0/22",
    "org": "org_" + "a" * 20,
    "base": "bcdfghjklmnp.apps.example.test",
    "auth_url": "https://auth.example.test",
    "username": "night-one@example.test",
    "drill": {
        "api_url": "https://api.example.test",
        "app_id": "app_" + "d" * 20,
        "env_id": "env_" + "d" * 20,
        "host": "drill.bcdfghjklmnp.apps.example.test",
    },
}
CELL_TWO: dict[str, Any] = {
    **CELL_ONE,
    "project": "ssc-c-two",
    "label": "cell two",
    "agent_url": "https://agent-two.example.test",
    "app_url": "https://app-two.example.test",
    "gateway_url": "https://gateway-two-uc.a.run.app",
    "range": "10.30.0.0/22",
    "base": "qrstvwxzbcdf.apps.example.test",
    "datagw_url": "https://datagw-two.example.test",
    "datagw_connection": "con_" + "a1b2c3d4e5f6g7h8i9j0",
}


def text(*cell: dict[str, Any]) -> str:
    return json.dumps(list(cell))


def broken(**changes: Any) -> str:
    body = copy.deepcopy(CELL_ONE)
    for key, value in changes.items():
        if value is None:
            del body[key]
        else:
            body[key] = value
    return text(body)


def test_two_cells_parse_with_every_key() -> None:
    one, two = cells.parse(text(CELL_ONE, CELL_TWO))
    assert (one.project, one.label, one.datagw_url) == ("ssc-c-one", "cell one", None)
    assert one.drill is not None
    assert one.drill.host == "drill.bcdfghjklmnp.apps.example.test"
    assert two.datagw_connection == "con_a1b2c3d4e5f6g7h8i9j0"
    assert two.hosts == [
        "alpha.qrstvwxzbcdf.apps.example.test",
        "bravo.qrstvwxzbcdf.apps.example.test",
    ]


@pytest.mark.parametrize("key", cells.REQUIRED)
def test_a_missing_required_key_is_named(key: str) -> None:
    with pytest.raises(cells.CellsError, match=rf'SSC_NIGHT_CELLS\[0\]: missing "{key}"'):
        cells.parse(broken(**{key: None}))


def test_the_second_cell_is_named_by_its_position() -> None:
    second = {k: v for k, v in CELL_TWO.items() if k != "org"}
    with pytest.raises(cells.CellsError, match=r'SSC_NIGHT_CELLS\[1\]: missing "org"'):
        cells.parse(text(CELL_ONE, second))


@pytest.mark.parametrize(
    ("given", "match"),
    [
        (None, "is not set"),
        ("", "is not set"),
        ("{not json", "is not valid JSON"),
        ("{}", "non-empty array"),
        ("[]", "non-empty array"),
        ("[1]", r"\[0\]: not an object"),
        (json.dumps([CELL_ONE, CELL_TWO, {**CELL_TWO, "project": "ssc-c-three"}]), "at most 2"),
        (json.dumps([CELL_ONE, CELL_ONE]), "same project"),
        (broken(extra="x"), "unknown key 'extra'"),
        (broken(project="  "), 'missing "project"'),
        (broken(project="a\nb"), "line break"),
        (broken(auth_url="http://auth.example.test"), '"auth_url" is not an https URL'),
        (broken(datagw_url="https://datagw.example.test"), "both"),
        (broken(datagw_connection="con_" + "a" * 20), "both"),
        (
            broken(datagw_url="https://d.example.test", datagw_connection="probe"),
            "not a con_<20> connection id",
        ),
        (broken(drill="x"), '"drill" is not an object'),
        (broken(drill={**CELL_ONE["drill"], "extra": "x"}), r"drill: unknown key 'extra'"),
        (broken(drill={"api_url": "https://api.example.test"}), r'drill: missing "app_id"'),
    ],
)
def test_a_mistake_fails_with_a_clear_message(given: str | None, match: str) -> None:
    with pytest.raises(cells.CellsError, match=match):
        cells.parse(given)


def test_a_message_never_carries_a_value() -> None:
    secret = "do-not-echo-this"
    with pytest.raises(cells.CellsError) as raised:
        cells.parse(broken(auth_url=secret))
    assert secret not in str(raised.value)
    with pytest.raises(cells.CellsError) as raised:
        cells.parse("{" + secret)
    assert secret not in str(raised.value)


def test_plan_names_the_cells_and_whether_there_is_a_peer() -> None:
    both = cells.section(cells.parse(text(CELL_ONE, CELL_TWO)), "plan")
    assert both == {
        "count": "2",
        "projects": "ssc-c-one,ssc-c-two",
        "positions": "[1, 2]",
        "peer": "true",
    }
    assert cells.section(cells.parse(text(CELL_ONE)), "plan")["peer"] == "false"


def test_probes_get_the_other_cell_as_their_peer_and_the_data_gateway_when_set() -> None:
    both = cells.parse(text(CELL_ONE, CELL_TWO))
    first = cells.section(both, "probes", 0)
    assert first == {
        "SSC_PROBE_PROJECT": "ssc-c-one",
        "SSC_PROBE_AGENT_URL": "https://agent-one.example.test",
        "SSC_PROBE_TLS_HOST": "alpha.bcdfghjklmnp.apps.example.test",
        "SSC_PROBE_PEER_APP_URL": "https://app-two.example.test",
        "SSC_PROBE_PEER_GATEWAY_URL": "https://gateway-two-uc.a.run.app",
        "SSC_PROBE_PEER_RANGE": "10.30.0.0/22",
        "SSC_PROBE_PEER_PROJECT": "ssc-c-two",
    }
    second = cells.section(both, "probes", 1)
    assert second["SSC_PROBE_PEER_PROJECT"] == "ssc-c-one"
    assert second["SSC_PROBE_DATAGW_URL"] == "https://datagw-two.example.test"
    assert second["SSC_PROBE_DATAGW_CONNECTION"] == "con_a1b2c3d4e5f6g7h8i9j0"


def test_one_cell_has_no_peer() -> None:
    alone = cells.parse(text(CELL_ONE))
    assert not any("PEER" in name for name in cells.section(alone, "probes"))
    assert "SSC_ISO_PEER_BASE" not in cells.section(alone, "browser")


def test_browser_and_drill_settings() -> None:
    both = cells.parse(text(CELL_ONE, CELL_TWO))
    browser = cells.section(both, "browser", 0)
    assert browser["SSC_ISO_CELL1_BASE"] == "bcdfghjklmnp.apps.example.test"
    assert browser["SSC_ISO_PEER_BASE"] == "qrstvwxzbcdf.apps.example.test"
    assert browser["SSC_ISO_GATEWAY_RUN_APP"] == "gateway-one-uc.a.run.app"
    assert (
        browser["SSC_NIGHT_HOSTS"]
        == "alpha.bcdfghjklmnp.apps.example.test,bravo.bcdfghjklmnp.apps.example.test"
    )
    assert browser["SSC_NIGHT_USERNAME"] == "night-one@example.test"
    assert (browser["SSC_NIGHT_PROJECT"], browser["SSC_NIGHT_PEER"]) == ("ssc-c-one", "true")
    assert cells.section(cells.parse(text(CELL_ONE)), "browser")["SSC_NIGHT_PEER"] == "false"
    drill = cells.section(both, "drill", 0)
    assert drill["SSC_DRILL_PROJECT"] == "ssc-c-one"
    assert drill["SSC_DRILL_ORG_ID"] == "org_" + "a" * 20
    assert drill["SSC_NIGHT_HOSTS"].endswith(",drill.bcdfghjklmnp.apps.example.test")
    assert drill["SSC_NIGHT_HOSTS"].startswith("alpha.")


def test_a_cell_without_a_drill_cannot_run_one() -> None:
    both = cells.parse(text({k: v for k, v in CELL_ONE.items() if k != "drill"}))
    with pytest.raises(cells.CellsError, match='no "drill" object'):
        cells.section(both, "drill", 0)


def test_a_position_outside_the_cells_is_refused() -> None:
    with pytest.raises(cells.CellsError, match="--position 3"):
        cells.section(cells.parse(text(CELL_ONE)), "probes", 2)


def test_main_prints_lines_and_exits_one_on_a_mistake(capsys: pytest.CaptureFixture[str]) -> None:
    ok = {cells.ENV: text(CELL_ONE, CELL_TWO)}
    assert cells.main(["plan"], ok) == 0
    assert "peer=true\n" in capsys.readouterr().out
    assert cells.main(["probes", "--position", "2"], ok) == 0
    assert "SSC_PROBE_PEER_PROJECT=ssc-c-one\n" in capsys.readouterr().out
    assert cells.main(["plan"], {cells.ENV: broken(org=None)}) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == 'cells: SSC_NIGHT_CELLS[0]: missing "org"\n'
