"""SSC-091: where does creating the DNS sinkhole's rules slow down?

Creates a throwaway response policy ``ssc-exp091`` in ``ssc-platform-0`` (no network attached, so no
VPC is affected) and times response policy rule creation, to tell apart three causes:

* Cloud DNS serialises changes to one policy: ten creates at once take about ten times one create
  and nothing returns a 429.
* A per-minute write quota: creates run fast, then answer 429 and name the quota.
* Neither: one create is itself slow, or concurrent creates are fast and the cause is elsewhere.

Standard library only; the access token comes from ``gcloud auth print-access-token``. It never
touches a project other than ``ssc-platform-0`` or a name that does not start with ``ssc-exp091``,
and sends at most 200 rule creations in all. Cleanup (E6) runs even when a step fails.

    python3 sinkhole.py --dry-run          # print every call, send none
    python3 sinkhole.py --steps E0         # read only
    python3 sinkhole.py                    # E0 to E6, results in results.json
    python3 sinkhole.py --cleanup-only     # delete every ssc-exp091* rule and policy
"""

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

PROJECT: Final = "ssc-platform-0"
PREFIX: Final = "ssc-exp091"
POLICY: Final = PREFIX
DNS: Final = f"https://dns.googleapis.com/dns/v1/projects/{PROJECT}"
POLICIES: Final = f"{DNS}/responsePolicies"
BATCH: Final = "https://dns.googleapis.com/batch"
QUOTAS: Final = (
    f"https://cloudquotas.googleapis.com/v1/projects/{PROJECT}/locations/global"
    "/services/dns.googleapis.com/quotaInfos"
)
ALLOWED: Final = (DNS + "/", QUOTAS)
CAP: Final = 200
BATCH_RESERVE: Final = 5  # E4 leaves room for E5's five
TIMEOUT: Final = 180
STEPS: Final = ("E0", "E1", "E2", "E3", "E4", "E5", "E6")
RESULTS: Final = Path(__file__).with_name("results.json")

Opener = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, dict[str, str], str]]


def http_opener(
    method: str, url: str, data: bytes | None, headers: dict[str, str]
) -> tuple[int, dict[str, str], str]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {}, str(exc)


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    seconds: float
    body: Any
    headers: dict[str, str]


class Refused(RuntimeError):
    pass


class Api:
    """Every call goes through here, so the project and name fences cannot be bypassed."""

    def __init__(self, token: Callable[[], str], opener: Opener, *, dry_run: bool) -> None:
        self._token = token
        self._opener = opener
        self.dry_run = dry_run
        self.calls: list[dict[str, Any]] = []
        self.rule_calls = 0
        self._lock = threading.Lock()
        self._start = time.monotonic()

    @staticmethod
    def guard(method: str, url: str, body: Any = None) -> None:
        if url != BATCH and not url.startswith(ALLOWED):
            raise Refused(f"{url} is not in {PROJECT}")
        if url == BATCH or method == "GET":
            return
        if url == POLICIES and method == "POST":
            if not str((body or {}).get("responsePolicyName", "")).startswith(PREFIX):
                raise Refused("a policy not named ssc-exp091*")
            return
        if not url.startswith(f"{POLICIES}/{PREFIX}"):
            raise Refused(f"{method} {url} is not under a ssc-exp091* policy")

    def send(
        self,
        method: str,
        url: str,
        body: Any = None,
        *,
        raw: bytes | None = None,
        content_type: str = "application/json",
        step: str = "",
    ) -> Reply:
        self.guard(method, url, body)
        is_rule = method == "POST" and url.endswith("/rules")
        with self._lock:
            if is_rule:
                if self.rule_calls >= CAP:
                    raise Refused(f"{CAP} rule creations already sent")
                self.rule_calls += 1
            started_at = time.monotonic() - self._start
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if self.dry_run:
            shown = json.dumps(body) if body else (raw.decode() if raw else "")
            print(f"DRY RUN {method} {url}" + (f"  {shown}" if shown else ""))  # noqa: T201
            return Reply(200, 0.0, {}, {})
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": content_type,
            "X-Goog-User-Project": PROJECT,
        }
        begin = time.monotonic()
        status, response_headers, text = self._opener(method, url, data, headers)
        seconds = time.monotonic() - begin
        try:
            parsed: Any = json.loads(text) if text.strip() else {}
        except ValueError:
            parsed = {"raw": text[:4000]}
        reply = Reply(status, seconds, parsed, response_headers)
        retry_after = {k.lower(): v for k, v in response_headers.items()}.get("retry-after")
        with self._lock:
            self.calls.append(
                {
                    "step": step,
                    "method": method,
                    "path": url.removeprefix("https://"),
                    "at": round(started_at, 3),
                    "seconds": round(seconds, 3),
                    "status": status,
                    "retry_after": retry_after,
                }
            )
        return reply


def gcloud_token() -> str:
    out = subprocess.run(  # noqa: S603
        ["gcloud", "auth", "print-access-token"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def rule_body(tag: str) -> dict[str, Any]:
    """The same shape as the cell's ``*.<tld>.`` rule: a CNAME to the sinkhole's name."""
    name = f"{PREFIX}-{tag}"
    dns_name = f"*.{name}."
    return {
        "ruleName": name,
        "dnsName": dns_name,
        "localData": {
            "localDatas": [
                {
                    "name": dns_name,
                    "type": "CNAME",
                    "ttl": 300,
                    "rrdatas": [f"sinkhole.{PREFIX}."],
                }
            ]
        },
    }


def create_rule(api: Api, tag: str, step: str) -> Reply:
    return api.send("POST", f"{POLICIES}/{POLICY}/rules", rule_body(tag), step=step)


def at_once(count: int, work: Callable[[int], Reply]) -> tuple[list[Reply], float]:
    """``count`` calls released together; returns the replies and the wall time."""
    barrier = threading.Barrier(count)

    def go(i: int) -> Reply:
        barrier.wait()
        return work(i)

    begin = time.monotonic()
    with ThreadPoolExecutor(max_workers=count) as pool:
        replies = list(pool.map(go, range(count)))
    return replies, time.monotonic() - begin


def summary(replies: list[Reply]) -> dict[str, Any]:
    seconds = sorted(r.seconds for r in replies)
    statuses: dict[str, int] = {}
    for r in replies:
        statuses[str(r.status)] = statuses.get(str(r.status), 0) + 1
    return {
        "count": len(replies),
        "statuses": statuses,
        "seconds_sorted": [round(s, 3) for s in seconds],
        "median": round(statistics.median(seconds), 3) if seconds else None,
        "max": round(seconds[-1], 3) if seconds else None,
    }


def error_of(reply: Reply) -> Any:
    return reply.body.get("error") if isinstance(reply.body, dict) else reply.body


def e0(api: Api, results: dict[str, Any]) -> None:
    """Read only: the quota listing, and whether a policy of ours is left over."""
    quotas = api.send("GET", f"{QUOTAS}?pageSize=200", step="E0")
    infos: list[Any] = quotas.body.get("quotaInfos", []) if isinstance(quotas.body, dict) else []
    wanted = [i for i in infos if "esponse" in json.dumps(i) or "olicy" in json.dumps(i)]
    policies = api.send("GET", POLICIES, step="E0")
    names = [p.get("responsePolicyName") for p in (policies.body or {}).get("responsePolicies", [])]
    results["E0"] = {
        "quota_status": quotas.status,
        "quota_error": error_of(quotas) if quotas.status != 200 else None,
        "quota_infos_total": len(infos),
        "quota_infos_response_policy": wanted,
        "policies": names,
        "leftover": [n for n in names if str(n).startswith(PREFIX)],
    }


def e1(api: Api, results: dict[str, Any]) -> None:
    reply = api.send(
        "POST",
        POLICIES,
        {"responsePolicyName": POLICY, "description": "SSC-091 throwaway experiment"},
        step="E1",
    )
    results["E1"] = {
        "status": reply.status,
        "error": error_of(reply) if reply.status != 200 else None,
    }
    if reply.status != 200 and not api.dry_run:
        raise Refused(
            f"policy not created ({reply.status}); run --cleanup-only first if {POLICY} exists"
        )


def e2(api: Api, results: dict[str, Any]) -> None:
    replies = [create_rule(api, f"e2-{i}", "E2") for i in range(10)]
    results["E2"] = summary(replies) | {
        "first_error": next((error_of(r) for r in replies if r.status != 200), None)
    }


def e3(api: Api, results: dict[str, Any]) -> None:
    replies, wall = at_once(10, lambda i: create_rule(api, f"e3-{i}", "E3"))
    results["E3"] = summary(replies) | {
        "wall": round(wall, 3),
        "first_error": next((error_of(r) for r in replies if r.status != 200), None),
    }


def e4(api: Api, results: dict[str, Any], concurrency: int) -> None:
    """Waves of 8, 16, then ``concurrency`` creates at once, no client retry; stops at the first
    429 (or any other error) or at the cap on rules sent."""
    sizes = [8, 16, *([concurrency] * 100)]
    waves: list[dict[str, Any]] = []
    started = time.monotonic()
    ok = 0
    stop: str | None = None
    index = 0
    for size in sizes:
        size = min(size, CAP - BATCH_RESERVE - api.rule_calls)
        if size <= 0:
            stop = f"cap of {CAP - BATCH_RESERVE} rules sent (the rest is E5's)"
            break
        base = index
        index += size
        replies, wall = at_once(size, lambda i, base=base: create_rule(api, f"e4-{base + i}", "E4"))
        ok += sum(1 for r in replies if r.status == 200)
        bad = [r for r in replies if r.status != 200]
        waves.append(
            summary(replies)
            | {
                "size": size,
                "wall": round(wall, 3),
                "elapsed": round(time.monotonic() - started, 3),
                "first_error": error_of(bad[0]) if bad else None,
                "retry_after": bad[0].headers.get("Retry-After") if bad else None,
            }
        )
        print(f"E4 wave of {size}: {summary(replies)['statuses']} in {wall:.1f}s")  # noqa: T201
        if bad:
            stop = f"status {bad[0].status} in a wave of {size}"
            break
    elapsed = time.monotonic() - started
    results["E4"] = {
        "waves": waves,
        "created": ok,
        "elapsed": round(elapsed, 3),
        "rules_per_minute": round(ok / elapsed * 60, 1) if elapsed else None,
        "stopped": stop,
    }


def e5(api: Api, results: dict[str, Any]) -> None:
    """One multipart batch of 5 creates to the generic batch URL."""
    boundary = "ssc_exp091_batch"
    parts: list[str] = []
    for i in range(5):
        path = f"/dns/v1/projects/{PROJECT}/responsePolicies/{POLICY}/rules"
        Api.guard("POST", f"https://dns.googleapis.com{path}", rule_body(f"e5-{i}"))
        parts.append(
            f"--{boundary}\r\nContent-Type: application/http\r\nContent-ID: <item{i}>\r\n\r\n"
            f"POST {path} HTTP/1.1\r\nContent-Type: application/json\r\n\r\n"
            f"{json.dumps(rule_body(f'e5-{i}'))}\r\n"
        )
    payload = ("".join(parts) + f"--{boundary}--\r\n").encode()
    if api.rule_calls + 5 > CAP:
        raise Refused("no room under the cap for the batch")
    api.rule_calls += 5
    reply = api.send(
        "POST",
        BATCH,
        raw=payload,
        content_type=f"multipart/mixed; boundary={boundary}",
        step="E5",
    )
    results["E5"] = {
        "status": reply.status,
        "seconds": round(reply.seconds, 3),
        "body": reply.body if not isinstance(reply.body, str) else reply.body[:4000],
    }


def pages(api: Api, url: str, key: str, step: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    token = ""
    while True:
        reply = api.send("GET", url + (f"?pageToken={token}" if token else ""), step=step)
        if reply.status != 200:
            raise Refused(f"list {url} failed: {reply.status} {error_of(reply)}")
        found += reply.body.get(key, [])
        token = reply.body.get("nextPageToken", "")
        if not token:
            return found


def delete_retrying(api: Api, url: str) -> int:
    """Cleanup must finish, so it retries 429s and server errors with a growing wait."""
    status = 0
    for attempt in range(12):
        status = api.send("DELETE", url, step="E6").status
        if status in (200, 204, 404) or api.dry_run:
            return status
        time.sleep(min(2**attempt, 30))
    return status


def e6(api: Api, results: dict[str, Any]) -> None:
    """Deletes every rule of every ``ssc-exp091*`` policy, the policies, and lists to confirm."""
    victims = [
        str(p["responsePolicyName"])
        for p in pages(api, POLICIES, "responsePolicies", "E6")
        if str(p.get("responsePolicyName", "")).startswith(PREFIX)
    ]
    deleted_rules = 0
    failed: list[str] = []
    for policy in victims:
        rules = [
            str(r["ruleName"])
            for r in pages(api, f"{POLICIES}/{policy}/rules", "responsePolicyRules", "E6")
        ]
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(
                pool.map(
                    lambda r, p=policy: delete_retrying(api, f"{POLICIES}/{p}/rules/{r}"), rules
                )
            )
        deleted_rules += sum(1 for s in statuses if s in (200, 204, 404))
        failed += [r for r, s in zip(rules, statuses, strict=True) if s not in (200, 204, 404)]
        if delete_retrying(api, f"{POLICIES}/{policy}") not in (200, 204, 404):
            failed.append(policy)
    left = [
        p
        for p in pages(api, POLICIES, "responsePolicies", "E6")
        if str(p.get("responsePolicyName", "")).startswith(PREFIX)
    ]
    results["E6"] = {
        "policies_found": victims,
        "rules_deleted": deleted_rules,
        "failed": failed,
        "left_after": [p.get("responsePolicyName") for p in left],
        "clean": not failed and not left,
    }
    print(
        f"E6 cleanup: {deleted_rules} rules, {len(victims)} policies; clean={results['E6']['clean']}"
    )  # noqa: T201


def reading(results: dict[str, Any]) -> list[str]:
    """A hint at which of the three causes the numbers point to. Read the numbers too."""
    out: list[str] = []
    e2r, e3r, e4r = results.get("E2"), results.get("E3"), results.get("E4")
    rate_limited = [
        s
        for r in (e3r, e4r)
        if r
        for s in ([r["statuses"]] if "statuses" in r else [w["statuses"] for w in r["waves"]])
        if "429" in s
    ]
    if rate_limited:
        out.append(
            "QUOTA: a 429 came back. See E4 first_error and retry_after for the quota's name; "
            f"rules per minute before it: {e4r.get('rules_per_minute') if e4r else 'n/a'}."
        )
    if e2r and e3r and e2r["median"]:
        ratio = e3r["wall"] / e2r["median"]
        if not rate_limited and ratio >= 6:
            out.append(
                f"SERIALISED per policy: 10 at once took {e3r['wall']}s, {ratio:.1f}x one create "
                f"({e2r['median']}s). No client concurrency will help."
            )
        elif not rate_limited and e2r["median"] > 10:
            out.append(f"SLOW per call: one create takes {e2r['median']}s by itself.")
        elif not rate_limited:
            out.append(
                f"PARALLEL is fine: 10 at once took {e3r['wall']}s ({ratio:.1f}x one create, "
                f"{e2r['median']}s). Look elsewhere (the provider's Read after create, the engine)."
            )
    if not out:
        out.append("No reading: run E2 and E3 (and E4).")
    return out


def run(args: argparse.Namespace, api: Api) -> int:
    chosen = [s for s in STEPS if s in args.steps]
    needs_policy = any(s in chosen for s in ("E2", "E3", "E4", "E5"))
    if needs_policy and "E1" not in chosen:
        print("E2 to E5 need the policy: adding E1")  # noqa: T201
        chosen.insert(0, "E1")
    results: dict[str, Any] = {
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": PROJECT,
        "policy": POLICY,
        "dry_run": args.dry_run,
    }

    def save() -> None:
        results["calls"] = api.calls
        if not args.dry_run:
            RESULTS.write_text(json.dumps(results, indent=2, default=str) + "\n")

    created = any(s in chosen for s in ("E1", "E2", "E3", "E4", "E5"))
    failure: str | None = None
    try:
        for step in [s for s in STEPS if s in chosen and s != "E6"]:
            print(f"--- {step}")  # noqa: T201
            start = time.monotonic()
            try:
                match step:
                    case "E0":
                        e0(api, results)
                    case "E1":
                        e1(api, results)
                    case "E2":
                        e2(api, results)
                    case "E3":
                        e3(api, results)
                    case "E4":
                        e4(api, results, args.concurrency)
                    case _:
                        e5(api, results)
            except Refused as exc:
                failure = f"{step}: {exc}"
                results[step] = results.get(step, {}) | {"refused": str(exc)}
                break
            print(
                f"{step} done in {time.monotonic() - start:.1f}s: {json.dumps(results[step])[:300]}"
            )  # noqa: T201
            save()
    finally:
        if created or "E6" in chosen:
            print("--- E6")  # noqa: T201
            try:
                e6(api, results)
            except Refused as exc:
                results["E6"] = {"refused": str(exc), "clean": False}
                failure = failure or f"E6: {exc}"
        results["reading"] = reading(results)
        results["rule_creations_sent"] = api.rule_calls
        save()
    for line in results["reading"]:
        print(line)  # noqa: T201
    print(
        f"rule creations sent: {api.rule_calls}; results in {RESULTS if not args.dry_run else '(dry run)'}"
    )  # noqa: T201
    if failure:
        print(f"stopped: {failure}", file=sys.stderr)  # noqa: T201
        return 1
    return 0 if results.get("E6", {"clean": True}).get("clean", True) else 1


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--steps", default=",".join(STEPS), help="comma list of E0..E6")
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--concurrency", type=int, default=32, help="E4's largest wave")
    args = parser.parse_args(argv)
    if args.project != PROJECT:
        parser.error(f"refusing project {args.project}: only {PROJECT}")
    steps = {s.strip().upper() for s in args.steps.split(",") if s.strip()}
    if not steps <= set(STEPS):
        parser.error(f"unknown steps {sorted(steps - set(STEPS))}")
    args.steps = {"E6"} if args.cleanup_only else steps
    args.concurrency = max(1, min(args.concurrency, 64))
    return args


def main(argv: list[str], opener: Opener = http_opener) -> int:
    args = parse(argv)
    api = Api(gcloud_token, opener, dry_run=args.dry_run)
    return run(args, api)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
