"""The nightly walk of every suffix (CD-7.2), against a fake fetcher."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from ssc_conformance import evidence as ev
from ssc_conformance import suffix_walk as sw
from ssc_conformance.matrix import SUFFIXES
from ssc_conformance.nightly import NightlyError
from ssc_contracts.snapshot import FORMAT_V1, SnapshotDoc

ORG = "org_" + "a" * 20
APP = "app_" + "l" * 20
PROD, PREVIEW, OFF = "env_" + "p" * 20, "env_" + "v" * 20, "env_" + "o" * 20
BASE = "bcdfghjklmnp.apps.test"
CUSTOM = "tools.acme.example"
AUTH = "https://auth.example.test"
NOW = datetime(2026, 10, 28, tzinfo=UTC)
PROJECT = "ssc-c-bcdfghjklmnp"


def snapshot(**changes: Any) -> dict[str, Any]:
    env = {"app_id": APP, "floor": "user", "status": "active"}
    doc: dict[str, Any] = {
        "format": FORMAT_V1,
        "org_id": ORG,
        "version": 3,
        "compiled_at": "2026-10-28T00:00:00Z",
        "environments": {
            PROD: {**env, "name": "prod"},
            PREVIEW: {**env, "name": "preview"},
            OFF: {**env, "name": "prod", "app_id": "app_" + "o" * 20, "status": "disabled"},
        },
        "hosts": {"ledger": PROD, "ledger--preview": PREVIEW, "old": OFF},
        "grants": {},
        "groups_by_user": {},
        "users": {},
        "ceiling": None,
    }
    doc.update(changes)
    return doc


def flipped() -> SnapshotDoc:
    return SnapshotDoc.model_validate(
        snapshot(
            suffix=CUSTOM,
            previous_suffixes=[
                {"suffix": "gone.acme.example", "until": "2026-10-27T00:00:00Z"},
                {"suffix": BASE, "until": "2026-11-27T00:00:00Z"},
            ],
        )
    )


class FakeFetch:
    """The internet as the gateway answers it: login on the current suffix, a 307 on a previous
    one, CAA on the customer zone; ``answers`` overrides a URL."""

    def __init__(self, current: str, caa: tuple[str, ...] = (CUSTOM,)) -> None:
        self.current = current
        self.caa = caa
        self.answers: dict[str, sw.Reply] = {}
        self.asked: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, headers: Mapping[str, str]) -> sw.Reply:
        self.asked.append((url, dict(headers)))
        if url in self.answers:
            return self.answers[url]
        if url.startswith(sw.DOH):
            name = url.split("name=", 1)[1].split("&", 1)[0]
            records = [{"type": 257, "data": sw.CAA}] if name in self.caa else []
            return sw.Reply(200, {}, json.dumps({"Status": 0, "Answer": records}).encode())
        host = url.removeprefix("https://").removesuffix("/")
        label, suffix = host.split(".", 1)
        if suffix == self.current:
            return sw.Reply(302, {"location": f"{AUTH}/login?org={ORG}"})
        return sw.Reply(
            307, {"location": f"https://{label}.{self.current}/", "cache-control": "no-store"}
        )


class Days:
    def __init__(self, left: float = 60.0) -> None:
        self.left = left
        self.seen: list[str] = []

    async def __call__(self, host: str) -> float:
        self.seen.append(host)
        return self.left


async def run(fetch: FakeFetch, doc: SnapshotDoc, **kw: Any) -> sw.Walk:
    return await sw.walk(
        fetch, doc, base=BASE, auth_url=AUTH, now=NOW, days_left=kw.pop("days_left", Days()), **kw
    )


async def test_the_default_suffix_alone_answers_login_and_asks_no_caa() -> None:
    fetch = FakeFetch(BASE)
    out = await run(fetch, SnapshotDoc.model_validate(snapshot()))
    assert out.failures == ()
    assert out.lines == (f"{BASE} (current): 2 hosts answer, certificate 60 days, CAA not asked",)
    assert [u for u, _ in fetch.asked] == [
        f"https://ledger.{BASE}/",
        f"https://ledger--preview.{BASE}/",
    ]
    assert out.as_result() == ev.Result(SUFFIXES, ev.OK, out.lines[0])


async def test_after_a_flip_the_customer_suffix_answers_and_the_default_redirects() -> None:
    fetch = FakeFetch(CUSTOM)
    left = Days()
    out = await run(fetch, flipped(), days_left=left)
    assert out.failures == ()
    assert out.lines == (
        f"{CUSTOM} (current): 2 hosts answer, certificate 60 days, CAA present",
        f"{BASE} (previous): 2 hosts redirect, certificate 60 days, CAA not asked",
    )
    asked = [u for u, _ in fetch.asked]
    assert f"{sw.DOH}?name={CUSTOM}&type=CAA" in asked
    assert not any("gone.acme.example" in u for u in asked)
    assert not any(f"name={BASE}" in u for u in asked)
    assert left.seen == [f"ledger.{CUSTOM}", f"ledger.{BASE}"]


async def test_a_200_passes_and_anything_else_on_the_current_suffix_fails() -> None:
    fetch = FakeFetch(CUSTOM)
    fetch.answers[f"https://ledger.{CUSTOM}/"] = sw.Reply(200, {})
    fetch.answers[f"https://ledger--preview.{CUSTOM}/"] = sw.Reply(
        302, {"location": "https://evil.example/login"}
    )
    out = await run(fetch, flipped())
    assert out.failures == (
        f"ledger--preview.{CUSTOM}: 302 to https://evil.example/login, not 200 or 302 to login",
    )
    assert out.as_result().status == ev.FAIL


@pytest.mark.parametrize(
    "reply",
    [
        sw.Reply(404, {}),
        sw.Reply(302, {"location": f"https://ledger.{CUSTOM}/", "cache-control": "no-store"}),
        sw.Reply(307, {"location": f"https://ledger.{CUSTOM}/"}),
        sw.Reply(307, {"location": f"https://other.{CUSTOM}/", "cache-control": "no-store"}),
    ],
)
async def test_a_previous_suffix_must_answer_a_no_store_307_to_the_same_label(
    reply: sw.Reply,
) -> None:
    fetch = FakeFetch(CUSTOM)
    fetch.answers[f"https://ledger.{BASE}/"] = reply
    out = await run(fetch, flipped())
    (failure,) = out.failures
    assert failure.startswith(f"ledger.{BASE}: {reply.status}")
    assert failure.endswith(f"not a no-store 307 to https://ledger.{CUSTOM}/")


async def test_a_certificate_under_fourteen_days_or_unverified_fails() -> None:
    out = await run(FakeFetch(CUSTOM), flipped(), days_left=Days(13.0))
    assert out.failures == (
        f"certificate: ledger.{CUSTOM} expires in 13 days, under 14",
        f"certificate: ledger.{BASE} expires in 13 days, under 14",
    )

    async def broken(host: str) -> float:
        raise NightlyError(f"{host} does not verify: SSLError")

    out = await run(FakeFetch(CUSTOM), flipped(), days_left=broken)
    assert out.failures[0] == f"certificate: ledger.{CUSTOM} does not verify: SSLError"
    assert f"{CUSTOM} (current): 2 hosts answer, certificate - days, CAA present" in out.lines


async def test_a_customer_suffix_without_the_caa_record_fails() -> None:
    out = await run(FakeFetch(CUSTOM, caa=()), flipped())
    assert out.failures == (f"CAA {CUSTOM}: {sw.CAA} not answered",)
    fetch = FakeFetch(CUSTOM)
    fetch.answers[f"{sw.DOH}?name={CUSTOM}&type=CAA"] = sw.Reply(503, {}, b"")
    assert (await run(fetch, flipped())).failures == (f"CAA {CUSTOM}: HTTP 503",)


async def test_an_org_with_no_active_host_fails() -> None:
    doc = SnapshotDoc.model_validate(snapshot(environments={}, hosts={}, grants={}))
    out = await sw.walk(FakeFetch(BASE), doc, base=BASE, auth_url=AUTH, now=NOW)
    assert out.failures == ("no active app host to walk",)


def published(doc: dict[str, Any]) -> tuple[dict[str, sw.Reply], bytes]:
    body = json.dumps(doc).encode()
    sha = hashlib.sha256(body).hexdigest()
    key = f"snapshots/{ORG}/v3-{sha[:12]}.json"
    pointer = json.dumps({"version": 3, "key": key, "digest": f"sha256:{sha}"}).encode()
    api = f"{sw.STORAGE_API}/b/{PROJECT}-cell/o/"
    return {
        f"{api}snapshots%2F{ORG}%2Flatest.json?alt=media": sw.Reply(200, {}, pointer),
        f"{api}snapshots%2F{ORG}%2Fv3-{sha[:12]}.json?alt=media": sw.Reply(200, {}, body),
    }, body


async def test_the_snapshot_is_read_from_the_cell_bucket_with_the_token() -> None:
    fetch = FakeFetch(BASE)
    fetch.answers, _ = published(snapshot(suffix=CUSTOM))
    doc = await sw.read_snapshot(fetch, f"{PROJECT}-cell", ORG, "tok")
    assert doc.suffix == CUSTOM
    assert all(h == {"authorization": "Bearer tok"} for _, h in fetch.asked)


async def test_a_snapshot_that_does_not_match_its_pointer_is_refused() -> None:
    fetch = FakeFetch(BASE)
    answers, _ = published(snapshot())
    key = next(k for k in answers if "latest" not in k)
    answers[key] = sw.Reply(200, {}, b"{}")
    fetch.answers = answers
    with pytest.raises(sw.WalkError, match="digest"):
        await sw.read_snapshot(fetch, f"{PROJECT}-cell", ORG, "tok")
    fetch.answers, _ = published({"format": "ssc-snapshot/v0"})
    with pytest.raises(sw.WalkError, match="not an ssc-snapshot/v1 document"):
        await sw.read_snapshot(fetch, f"{PROJECT}-cell", ORG, "tok")
    missing = FakeFetch(BASE)
    missing.answers = {k: sw.Reply(404, {}) for k in answers}
    with pytest.raises(sw.WalkError, match="latest.json: HTTP 404"):
        await sw.read_snapshot(missing, f"{PROJECT}-cell", ORG, "tok")


def environ(tmp_path: Path) -> dict[str, str]:
    return {
        "SSC_PROBE_PROJECT": PROJECT,
        "SSC_PROBE_ORG_ID": ORG,
        "SSC_PROBE_BASE": BASE,
        "SSC_PROBE_AUTH_URL": AUTH + "/",
        ev.EVIDENCE_ENV: str(tmp_path / "probes.json"),
    }


async def test_main_adds_its_proof_beside_the_probe_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSC_ACCESS_TOKEN", "tok")
    path = tmp_path / "probes.json"
    ev.write(path, ev.Evidence(PROJECT, True, (ev.Result("drift", ev.OK),)))
    fetch = FakeFetch(BASE)
    fetch.answers, _ = published(snapshot())
    out = await sw.main_async(environ(tmp_path), fetch=fetch, days_left=Days())
    assert out.failures == ()
    written = ev.read_file(path)
    assert written.peer is True
    assert [r.proof for r in written.results] == ["drift", SUFFIXES]
    assert written.results[1].status == ev.OK


async def test_main_reports_a_failure_when_the_snapshot_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSC_ACCESS_TOKEN", "tok")
    fetch = FakeFetch(BASE)
    fetch.answers = {k: sw.Reply(403, {}) for k in published(snapshot())[0]}
    out = await sw.main_async(environ(tmp_path), fetch=fetch, days_left=Days())
    assert out.failures == (f"snapshot: snapshots/{ORG}/latest.json: HTTP 403",)
    (result,) = ev.read_file(tmp_path / "probes.json").results
    assert (result.proof, result.status) == (SUFFIXES, ev.FAIL)


def test_the_settings_are_all_required() -> None:
    with pytest.raises(sw.WalkError, match="SSC_PROBE_BASE, SSC_PROBE_AUTH_URL"):
        sw.config_from_env({"SSC_PROBE_PROJECT": PROJECT, "SSC_PROBE_ORG_ID": ORG})
    cfg = sw.config_from_env(environ(Path("/x")))
    assert (cfg.bucket, cfg.auth_url) == (f"{PROJECT}-cell", AUTH)


async def test_the_http_fetcher_never_follows_a_redirect() -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "boom.test":
            raise httpx2.ConnectError("refused", request=request)
        return httpx2.Response(307, headers={"Location": "https://x.test/", "Cache-Control": "a"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as client:
        fetch = sw.http_fetch(client)
        reply = await fetch("https://a.test/", {})
        assert (reply.status, reply.headers["location"]) == (307, "https://x.test/")
        with pytest.raises(sw.WalkError, match=r"^https://boom.test/: ConnectError$"):
            await fetch("https://boom.test/?token=no", {})
