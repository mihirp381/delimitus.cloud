"""GA-6.5: a secret in the source is refused by the build's gitleaks scan: no image, no release.

``buildsecret --app <slug>``

The app is a new, never-deployed throwaway (``ga6secret``); only preview is used, because
``ssc deploy`` always targets preview. A secret meets three scans (``docs/trust/build-limits.md``):
``ssc deploy`` before upload and the control plane at bundle complete run the same
``ssc_bundle.secrets`` scan, then the build runs gitleaks (``ssc_agent/cloud_build.py``, exit 10
``SECRET_IN_BUNDLE``). To reach the build, the kit plants a value of a shape gitleaks knows and
``ssc_bundle.secrets`` does not: a GitHub token shape (rule ``github-pat``), made at run time from
random letters and digits, so it is no live token. It is written only into a temporary copy of
``apps/static`` (as ``config.js``), deleted after the deploy; the kit keeps only its fingerprint.

0. The app has no release and preview has no deployment; else the kit stops before deploying.
1. **[real]** ``ssc deploy --json`` of the copy: exit 1 (not 4: the CLI scan passed and the bundle
   was uploaded), ``SECRET_IN_BUNDLE`` from the build (``instance`` ``/v1/builds/bld_...``).
2. The build log (``ssc logs --source build``) has gitleaks' ``RuleID: github-pat`` and
   ``leaks found: 1``, and the value appears in nothing any command printed or the API answered.
3. Audit: ``build.started`` names the bundle, ``bundle.stored`` shows the control plane's re-scan
   accepted it, ``build.failed`` has ``failure_code`` ``SECRET_IN_BUNDLE``.
4. Still no release (a release is what carries an image digest) and no preview deployment.
5. Manual, read-only: the printed ``gcloud builds list`` shows the ``scan`` step failed, the later
   steps never ran and no image was pushed. It never sets the verdict.

Pass: checks 0 to 4 passed. Leaves behind the app's failed build and its stored bundle, which
holds the random value. The kit never prints or saves the value.
"""

import argparse
import hashlib
import json
import re
import secrets
import shutil
import string
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from proofrun import t8
from proofrun.common import (
    KIT,
    REGION,
    REPO,
    Done,
    Http,
    Outcome,
    Run,
    fence,
    fetch,
    run_command,
    ssc_json,
    ssc_prefix,
)

PROOF: Final = "GA-6.5"
CODE: Final = "SECRET_IN_BUNDLE"
RULE: Final = "github-pat"
TOKEN_PREFIX: Final = "gh" + "p_"
"""Split so that no line of the kit is itself a finding."""
TOKEN_BODY: Final = 36
ALPHABET: Final = string.ascii_letters + string.digits
FIXTURE: Final = KIT / "apps" / "static"
PLANTED: Final = "config.js"
DEPLOY_TIMEOUT_S: Final = 1500
"""The build's own timeout (1200 s) plus its queue time (300 s)."""
BLOCKED_EXIT: Final = 4
"""``ssc``'s exit for a refusal found before anything leaves the machine."""
LOG_TRIES: Final = 12
LOG_GAP_S: Final = 10.0
"""Build lines reach ``ssc logs`` up to about 26 s late (trust pack, app-access-limits)."""
LEAKS_LINE: Final = "leaks found: 1"
BUILD_ID: Final = re.compile(r"bld_[a-z0-9]{20}")
INSTANCE: Final = re.compile(r"/v1/builds/(bld_[a-z0-9]{20})")
_ANSI: Final = re.compile(r"\x1b\[[0-9;]*m")
NAMES: Final = {
    0: "before: the app has no release and preview has no deployment",
    1: "[real] ssc deploy: exit 1 with SECRET_IN_BUNDLE from the build, not the CLI's scan",
    2: "build log: gitleaks RuleID github-pat and leaks found: 1; the value shown nowhere",
    3: "audit: bundle.stored (re-scan passed), build.failed with failure_code SECRET_IN_BUNDLE",
    4: "after: still no release (so no image) and no preview deployment",
}
LEAVES_BEHIND: Final = (
    "leaves behind: the app's failed build and its stored bundle, which holds the random value "
    "(no live token); the app cannot be deleted"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app", required=True, help="a new, never-deployed app (ga6secret)")


def make_token(choice: Callable[[str], str] = secrets.choice) -> str:
    """A GitHub token shape from random letters and digits: gitleaks' ``github-pat``."""
    return TOKEN_PREFIX + "".join(choice(ALPHABET) for _ in range(TOKEN_BODY))


def fingerprint(value: str) -> str:
    """The first 12 hex digits of the value's SHA-256: all the kit keeps."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def stage(target: Path, value: str) -> list[str]:
    """A copy of ``apps/static`` in ``target``, plus ``config.js`` holding ``value``."""
    names = []
    for source in sorted(FIXTURE.iterdir()):
        shutil.copyfile(source, target / source.name)
        names.append(source.name)
    (target / PLANTED).write_text(f'export const upstream = "{value}";\n')
    return [*names, PLANTED]


@dataclass(frozen=True, slots=True)
class Check:
    """One numbered check; ``result`` is None when it could not be read."""

    n: int
    result: bool | None
    detail: str

    @property
    def word(self) -> str:
        return {True: "PASS", False: "FAIL", None: "not read"}[self.result]

    def line(self) -> str:
        return f"check {self.n} {self.word}: {NAMES[self.n]} ({self.detail})"

    def data(self) -> dict[str, Any]:
        return {"n": self.n, "name": NAMES[self.n], "result": self.word, "detail": self.detail}


def judge_empty(n: int, releases: Mapping[str, Any], status: Mapping[str, Any]) -> Check:
    """Checks 0 and 4: no release, and preview without a deployment."""
    count = len(releases.get("releases") or [])
    preview = [e for e in status.get("environments") or [] if e.get("name") == "preview"]
    if not preview:
        return Check(n, None, "ssc status lists no preview environment")
    current = preview[0].get("current_deployment_id")
    detail = f"{count} releases, preview deployment {current or 'none'}"
    return Check(n, count == 0 and current is None, detail)


def judge_deploy(done: Done) -> tuple[Check, str | None]:
    """Check 1 and the build id: the build refused it, not the CLI or the control plane."""
    error = _error_of(done.stdout)
    code = str(error.get("code") or "")
    found = INSTANCE.fullmatch(str(error.get("instance") or ""))
    build_id = found.group(1) if found else None
    if done.returncode == 0:
        return Check(1, False, "the deploy succeeded: nothing refused the secret"), build_id
    if done.returncode == BLOCKED_EXIT:
        return Check(1, False, f"exit 4 {code}: the CLI's own scan refused it before upload"), None
    if code == "WAIT_TIMED_OUT":
        return Check(1, None, f"exit {done.returncode}: stopped waiting for the build"), build_id
    detail = f"exit {done.returncode} {code or 'no error code'}, build {build_id or 'none'}"
    passed = done.returncode == 1 and code == CODE and build_id is not None
    return Check(1, passed, detail), build_id


def _error_of(stdout: str) -> dict[str, Any]:
    try:
        doc = json.loads(stdout)
    except ValueError:
        return {}
    error = doc.get("error") if isinstance(doc, dict) else None
    return error if isinstance(error, dict) else {}


def rule_lines(lines: Sequence[str]) -> tuple[bool, bool]:
    """Whether the build log holds gitleaks' ``RuleID: github-pat`` and ``leaks found: 1``."""
    plain = [_ANSI.sub("", line) for line in lines]
    rule = any(re.search(rf"RuleID:\s+{RULE}\b", line) for line in plain)
    return rule, any(LEAKS_LINE in line for line in plain)


def judge_log(lines: Sequence[str] | None, leaked: Sequence[str]) -> Check:
    """Check 2: gitleaks' finding in the build log, and the value nowhere."""
    if leaked:
        return Check(2, False, f"the value appeared in: {', '.join(leaked)}")
    if lines is None:
        return Check(2, None, "the build log could not be read")
    rule, leaks = rule_lines(lines)
    detail = f"{len(lines)} lines, RuleID {RULE} {'yes' if rule else 'no'}, "
    detail += f"'{LEAKS_LINE}' {'yes' if leaks else 'no'}, value shown nowhere"
    return Check(2, rule and leaks, detail)


def judge_audit(
    started: Sequence[Mapping[str, Any]],
    stored: Sequence[Mapping[str, Any]],
    failed: Sequence[Mapping[str, Any]],
) -> Check:
    """Check 3 from the ``build.started``, ``bundle.stored`` and ``build.failed`` rows."""
    bundle = next((str((e.get("after") or {}).get("bundle_id")) for e in started), None)
    codes = [(e.get("after") or {}).get("failure_code") for e in failed]
    if bundle is None or not failed:
        return Check(3, None, f"build.started {len(started)}, build.failed {len(failed)} rows")
    detail = (
        f"bundle {bundle}: bundle.stored {'yes' if stored else 'no'}; "
        f"build.failed failure_code {', '.join(str(c) for c in codes)}"
    )
    return Check(3, bool(stored) and CODE in codes, detail)


def verdict(checks: Sequence[Check]) -> bool | None:
    if checks[0].result is not True:
        return None
    if any(c.result is False for c in checks):
        return False
    return None if any(c.result is None for c in checks) else True


def check5_command(build_id: str | None) -> str:
    tag = build_id or "<bld_ id>"
    return (
        f"gcloud builds list --project=ssc-c-<label> --region={REGION} "
        f"--filter='tags={tag}' --format=json"
    )


@dataclass
class State:
    """The run so far. ``seen`` is every text a command printed or the API answered, checked
    for the value at the end and never saved."""

    run: Run
    http: Http
    slug: str
    value: str
    seen: list[tuple[str, str]] = field(default_factory=list)

    def keep(self, what: str, done: Done) -> Done:
        self.seen.append((what, done.stdout + "\n" + done.stderr))
        return done

    def ssc(self, what: str, *args: str) -> Done:
        return self.keep(what, self.run([*ssc_prefix(), *args], cwd=REPO))

    def ssc_doc(self, *args: str) -> dict[str, Any]:
        doc = ssc_json(self._recording(" ".join(args[:2])), *args)
        return doc if isinstance(doc, dict) else {}

    def _recording(self, what: str) -> Run:
        def run(
            argv: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
        ) -> Done:
            return self.keep(what, self.run(argv, cwd=cwd, env=env))

        return run

    def leaked(self) -> list[str]:
        body = self.value[len(TOKEN_PREFIX) :]
        hits = [what for what, text in self.seen if self.value in text or body in text]
        return sorted(set(hits))


def read_log(st: State, sleep: Callable[[float], None]) -> list[str] | None:
    """The preview's build log, polled until gitleaks' finding shows or the tries run out."""
    lines: list[str] | None = None
    for attempt in range(LOG_TRIES):
        if attempt:
            sleep(LOG_GAP_S)
        done = st.ssc(
            "ssc logs", "logs", st.slug, "--env", "preview", "--source", "build", "--json"
        )
        if done.returncode != 0:
            continue
        try:
            doc = json.loads(done.stdout)
        except ValueError:
            continue
        lines = [str(line.get("text", "")) for line in doc.get("lines") or []]
        if all(rule_lines(lines)):
            return lines
    return lines


def read_audit(st: State, build_id: str) -> Check:
    url, token = t8.control_api(st.run)
    fence(url)
    api = url.rstrip("/")

    def rows(action: str, kind: str, target: str) -> list[dict[str, Any]]:
        filters = {"action": action, "target_kind": kind, "target_id": target, "limit": 10}
        events = t8.search_audit(st.http, api, token, filters)
        st.seen.append((f"audit {action}", json.dumps(events)))
        return events

    started = rows("build.started", "build", build_id)
    bundle = next((str((e.get("after") or {}).get("bundle_id")) for e in started), None)
    stored = rows("bundle.stored", "bundle", bundle) if bundle else []
    return judge_audit(started, stored, rows("build.failed", "build", build_id))


def run(  # noqa: PLR0913, PLR0917  (every seam is injectable)
    args: argparse.Namespace,
    run: Run = run_command,
    http: Http = fetch,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print,
    make_value: Callable[[], str] = make_token,
) -> Outcome:
    fence(args.app)
    st = State(run, http, args.app, make_value())
    fp = fingerprint(st.value)
    data: dict[str, Any] = {"app": args.app, "fingerprint": fp, "rule": RULE}
    checks = [judge_empty(0, st.ssc_doc("releases", args.app), st.ssc_doc("status", args.app))]
    build_id: str | None = None
    if checks[0].result is True:
        say(f"[real] ssc deploy of a copy of apps/static with a planted {RULE} shape ({fp})")
        with tempfile.TemporaryDirectory(prefix="ga65-") as tmp:
            data["staged"] = stage(Path(tmp), st.value)
            timeout = str(DEPLOY_TIMEOUT_S)
            argv = ("deploy", "--app", args.app, tmp, "--timeout", timeout, "--json")
            deployed = st.ssc("ssc deploy", *argv)
        check1, build_id = judge_deploy(deployed)
        ended = build_id is not None and check1.result is not None
        lines = read_log(st, sleep) if ended else None
        check3 = read_audit(st, build_id) if ended else Check(3, None, "no ended build")
        after = (st.ssc_doc("releases", args.app), st.ssc_doc("status", args.app))
        check4 = judge_empty(4, *after)
        checks += [check1, judge_log(lines, st.leaked()), check3, check4]
    else:
        checks += [Check(n, None, "stopped before deploying") for n in range(1, 5)]
    passed = sum(c.result is True for c in checks)
    data.update(build_id=build_id, checks=[c.data() for c in checks])
    out = [c.line() for c in sorted(checks, key=lambda c: c.n)]
    out += [
        f"check 5 manual, read-only (never sets the verdict): {check5_command(build_id)}: the "
        "scan step FAILURE, plan/build/harden not run, no results.images",
        LEAVES_BEHIND,
    ]
    number = f"{build_id or 'no build'}: {passed} of {len(checks)} checks passed"
    return Outcome(PROOF, number, verdict(checks), out, data)
