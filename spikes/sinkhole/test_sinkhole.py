"""The experiment script against a fake Cloud DNS. No network, no gcloud.

cd infra && uv run pytest ../spikes/sinkhole -p no:cacheprovider
"""

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import sinkhole as sh  # noqa: E402


class FakeDns:
    """Three behaviours: ``fast``, ``serial`` (one change at a time per policy, ``delay`` each) and
    ``quota`` (answers 429 once ``limit`` creates were accepted)."""

    def __init__(self, mode: str, delay: float = 0.02, limit: int = 40) -> None:
        self.mode, self.delay, self.limit = mode, delay, limit
        self.policies: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.policy_lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []
        self.accepted = 0

    def __call__(
        self, method: str, url: str, data: bytes | None, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], str]:
        assert headers["X-Goog-User-Project"] == sh.PROJECT
        self.requests.append((method, url))
        if url.startswith(sh.QUOTAS):
            infos = [{"quotaId": "ResponsePolicyRuleWritesPerMinutePerRegion"}, {"quotaId": "x"}]
            return 200, {}, json.dumps({"quotaInfos": infos})
        if url == sh.BATCH:
            return 404, {}, json.dumps({"error": {"code": 404, "message": "no batch"}})
        tail = url.removeprefix(sh.POLICIES).split("?")[0]
        parts = [p for p in tail.split("/") if p]
        if not parts:
            if method == "GET":
                body = {"responsePolicies": [{"responsePolicyName": n} for n in self.policies]}
                return 200, {}, json.dumps(body)
            name = json.loads(data or b"{}")["responsePolicyName"]
            if name in self.policies:
                return 409, {}, json.dumps({"error": {"code": 409, "message": "exists"}})
            self.policies[name] = {}
            return 200, {}, "{}"
        policy = parts[0]
        if len(parts) == 1:
            assert method == "DELETE"
            if self.policies[policy]:
                return 400, {}, json.dumps({"error": {"message": "policy has rules"}})
            del self.policies[policy]
            return 204, {}, ""
        rules = self.policies[policy]
        if len(parts) == 2 and method == "GET":
            return 200, {}, json.dumps({"responsePolicyRules": [{"ruleName": r} for r in rules]})
        if len(parts) == 2:
            if self.mode == "quota":
                with self.lock:
                    if self.accepted >= self.limit:
                        err = {
                            "error": {
                                "code": 429,
                                "message": "Quota exceeded for ...WritesPerMinute",
                            }
                        }
                        return 429, {"Retry-After": "7"}, json.dumps(err)
            if self.mode == "serial":
                with self.policy_lock:
                    time.sleep(self.delay)
            else:
                time.sleep(self.delay)
            with self.lock:
                self.accepted += 1
                rules[json.loads(data or b"{}")["ruleName"]] = True
            return 200, {}, "{}"
        assert method == "DELETE"
        rules.pop(parts[2], None)
        return 204, {}, ""


def go(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake: FakeDns, *argv: str
) -> dict[str, Any]:
    out = tmp_path / "results.json"
    monkeypatch.setattr(sh, "RESULTS", out)
    monkeypatch.setattr(sh, "gcloud_token", lambda: "token")
    code = sh.main(list(argv), opener=fake)
    assert code == 0
    return json.loads(out.read_text())


def test_one_at_a_time_per_policy_reads_as_serialised(monkeypatch, tmp_path) -> None:
    fake = FakeDns("serial", delay=0.05)
    results = go(monkeypatch, tmp_path, fake, "--steps", "E0,E1,E2,E3")
    assert results["E3"]["wall"] >= 0.4
    assert results["reading"][0].startswith("SERIALISED")
    assert results["E0"]["quota_infos_total"] == 2
    assert len(results["E0"]["quota_infos_response_policy"]) == 1
    assert fake.policies == {}
    assert results["E6"] == {
        "policies_found": ["ssc-exp091"],
        "rules_deleted": 20,
        "failed": [],
        "left_after": [],
        "clean": True,
    }


def test_a_429_stops_the_ramp_and_reads_as_a_quota(monkeypatch, tmp_path) -> None:
    fake = FakeDns("quota", limit=40)
    results = go(monkeypatch, tmp_path, fake, "--steps", "E1,E2,E3,E4")
    e4 = results["E4"]
    assert "status 429" in e4["stopped"]
    assert e4["waves"][-1]["retry_after"] == "7"
    assert "WritesPerMinute" in e4["waves"][-1]["first_error"]["message"]
    assert results["reading"][0].startswith("QUOTA")
    assert fake.policies == {}


def test_parallel_creates_that_are_fast_read_as_fine_and_the_cap_holds(
    monkeypatch, tmp_path
) -> None:
    fake = FakeDns("fast", delay=0.005)
    results = go(monkeypatch, tmp_path, fake, "--steps", "E1,E2,E3,E4,E5")
    assert results["E4"]["stopped"].startswith(f"cap of {sh.CAP - sh.BATCH_RESERVE} rules")
    assert results["rule_creations_sent"] == sh.CAP
    assert results["E5"]["status"] == 404
    assert results["reading"][0].startswith("PARALLEL")
    posts = [u for m, u in fake.requests if m == "POST" and u.endswith("/rules")]
    assert len(posts) <= sh.CAP
    assert fake.policies == {}


def test_cleanup_only_removes_what_a_killed_run_left(monkeypatch, tmp_path) -> None:
    fake = FakeDns("fast")
    fake.policies = {
        "ssc-exp091": {"a": True, "b": True},
        "ssc-exp091-old": {},
        "ssc-cell": {"k": 1},
    }
    results = go(monkeypatch, tmp_path, fake, "--cleanup-only")
    assert results["E6"]["clean"] is True
    assert fake.policies == {"ssc-cell": {"k": 1}}
    deletes = [u for m, u in fake.requests if m == "DELETE"]
    assert all("/ssc-exp091" in u for u in deletes)


def test_a_leftover_policy_stops_the_run_and_is_cleaned(monkeypatch, tmp_path) -> None:
    fake = FakeDns("fast")
    fake.policies = {"ssc-exp091": {"old": True}}
    out = tmp_path / "results.json"
    monkeypatch.setattr(sh, "RESULTS", out)
    monkeypatch.setattr(sh, "gcloud_token", lambda: "token")
    assert sh.main(["--steps", "E1,E2"], opener=fake) == 1
    assert fake.policies == {}


def test_any_other_project_or_name_is_refused() -> None:
    with pytest.raises(SystemExit):
        sh.parse(["--project", "ristretto-506621"])
    with pytest.raises(SystemExit):
        sh.parse(["--project", "ssc-c-proofcell01"])
    with pytest.raises(sh.Refused):
        sh.Api.guard(
            "DELETE",
            "https://dns.googleapis.com/dns/v1/projects/ssc-c-x/responsePolicies/ssc-exp091",
        )
    with pytest.raises(sh.Refused):
        sh.Api.guard("DELETE", f"{sh.POLICIES}/ssc-cell")
    with pytest.raises(sh.Refused):
        sh.Api.guard("DELETE", f"{sh.POLICIES}/ssc-cell/rules/ssc-exp091-x")
    with pytest.raises(sh.Refused):
        sh.Api.guard("POST", sh.POLICIES, {"responsePolicyName": "ssc-cell"})
    with pytest.raises(sh.Refused):
        sh.Api.guard(
            "GET", "https://dns.googleapis.com/dns/v1/projects/ristretto-506621/responsePolicies"
        )


def test_dry_run_sends_nothing(capsys) -> None:
    def never(*_: Any) -> Any:
        raise AssertionError("sent")

    api = sh.Api(lambda: "x", never, dry_run=True)
    assert sh.run(sh.parse(["--dry-run", "--steps", "E1,E2,E5"]), api) == 0
    assert "DRY RUN POST" in capsys.readouterr().out
