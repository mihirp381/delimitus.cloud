"""The live adversarial sweep, run against the offline rig: every probe passes, a route missing
from ``KNOWN`` fails it, and the report goes to the file and the step summary."""

import json
from collections.abc import Iterator
from pathlib import Path

import httpx2
import jwt
import pytest
from adversarial_rig import Attacker, Victim, attacker, fingerprint, victim
from ssc_testkit import Dsns, SigningKey, control_db, new_signing_key
from test_mcp import serving, settings

from ssc_conformance import adversarial
from ssc_conformance.adversarial import FAIL, PASS, SKIP, Row
from ssc_control.api.openapi import build_spec


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with control_db() as d:
        yield d


@pytest.fixture(scope="module")
def signing_key() -> SigningKey:
    return new_signing_key()


@pytest.fixture(scope="module")
def rig(
    dsns: Dsns, signing_key: SigningKey, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[str, Attacker, Victim]]:
    blobs = tmp_path_factory.mktemp("blobs")
    with serving(settings(dsns, signing_key, 120, 1.0), blobs) as url:
        yield url, attacker(url, dsns, signing_key), victim(url, dsns, signing_key)


def test_the_sweep_passes_against_the_rig(
    rig: tuple[str, Attacker, Victim],
    dsns: Dsns,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, a, b = rig
    login = tmp_path / "login.json"
    login.write_text(json.dumps({"access_token": a.api.access, "refresh_token": a.api.refresh}))
    report, summary = tmp_path / "report.md", tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    args = ["--api", url, "--token", str(login), "--org", a.org.org_id, "--report", str(report)]
    args += ["--victim-app", b.app["id"], "--app", a.app["slug"]]
    args += ["--second-token", a.api_other.access]
    victims = fingerprint(dsns.superuser, (b.org.org_id,))
    assert adversarial.main(args) == 0, report.read_text()
    text = report.read_text()
    assert f"| {FAIL} |" not in text
    for group in ("1", "2", "4", "5", "6", "7"):
        assert f"| {group} |" in text
    assert "probes, 0 failed." in text
    assert summary.read_text() == text
    assert fingerprint(dsns.superuser, (b.org.org_id,)) == victims
    listed = httpx2.get(f"{url}/v1/approvals", headers={"Authorization": f"Bearer {a.human}"})
    opened = [
        r for r in listed.json()["approvals"] if r["subject_key"] == adversarial.NO_CONNECTION
    ]
    assert [r["state"] for r in opened] == ["cancelled"]


def test_a_marked_route_missing_from_known_fails(
    rig: tuple[str, Attacker, Victim], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, a, _ = rig
    gone = "PUT /v1/org/agent-policy"
    monkeypatch.setattr(
        adversarial, "KNOWN", {k: v for k, v in adversarial.KNOWN.items() if k != gone}
    )
    with httpx2.Client(
        base_url=url, headers={"Authorization": f"Bearer {a.api_other.access}"}
    ) as c:
        rows = adversarial.refusals(c, build_spec(), a.ids)
    assert Row("1", gone, "in KNOWN", "unmapped", FAIL) in rows


def test_an_unlisted_write_fails() -> None:
    spec = build_spec()
    spec["paths"]["/v1/apps/{app_id}/new-write"] = {"post": {"responses": {}}}
    assert "POST /v1/apps/{app_id}/new-write" in adversarial.unmarked_writes(spec)


def test_any_fail_exits_one_and_names_a_request_left_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [Row("1", "ok", "x", "x", PASS), Row("4", "off", "y", "skipped", SKIP)]
    rows.append(Row("6", "bad", "403 AGENT_SESSION_REFUSED", "200", FAIL))
    monkeypatch.setattr(adversarial, "sweep", lambda *_, **__: (rows, "apr_left"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    token = jwt.encode({"org": "org_x", "agent": True}, "k" * 32, algorithm="HS256")
    report = tmp_path / "r.md"
    args = ["--api", "http://x", "--token", token, "--org", "org_x", "--report", str(report)]
    assert adversarial.main(args) == 1
    text = report.read_text()
    assert "| 6 | `bad` | 403 AGENT_SESSION_REFUSED | 200 | FAIL |" in text
    assert "3 probes, 1 failed." in text
    assert "`apr_left`" in text


def test_the_token_must_be_for_the_org(tmp_path: Path) -> None:
    token = jwt.encode({"org": "org_x", "agent": True}, "k" * 32, algorithm="HS256")
    args = [
        "--api",
        "http://x",
        "--token",
        token,
        "--org",
        "org_y",
        "--report",
        str(tmp_path / "r"),
    ]
    assert adversarial.main(args) == 2
