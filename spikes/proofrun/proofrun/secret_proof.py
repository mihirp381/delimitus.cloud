"""GA-4.6: secrets pinned per deployment, rotation, never read back, and the deny rule.

``secrets --app <secrets fixture app> --label <cell label> [--env preview] [--control-sa <email>]``

The app is ``apps/secrets``: it answers ``/secret`` with a fingerprint of ``GA46_TOKEN`` (the first
12 hex digits of the SHA-256 of its bytes, and its length), never the value. The kit makes two
random values in memory, hands each to ``ssc secret set`` on stdin only, and keeps only their
fingerprints. It reads the app through its public host with the session cookie from the jar.
Only preview is used, because ``ssc deploy`` always targets preview.

1. **[real]** ``ssc secret set`` v1 (``--wait``). 2. **[real]** it is live: the set's own deployment
when something was live already, else ``ssc deploy`` of the fixture. 3. ``/secret`` answers v1's
fingerprint and length. 4. ``ssc secret list`` shows the name, the version and ``live_version``,
exactly those four fields, and neither the value nor its fingerprint appears in anything printed.
5. ``ssc secret --help`` lists only ``set`` and ``list``: no command reads a value back.
6. **[real]** ``ssc secret set`` v2 with no ``--wait``: it starts its own deployment (the route
starts one of the live release; no ``ssc deploy`` is run). 7. Right after, ``secret list`` has
``version`` v2 and ``live_version`` v1, and the app still answers v1: the running deployment keeps
its pin. 8. **[real]** ``live_version`` becomes v2 (polled every 10 s for 600 s) and the app answers
v2's fingerprint. 9. The cell's deny rule ``ssc-deny-secret-read`` denies
``secretmanager.versions.access`` to ``ssc-secret-intake`` with no condition (read-only
``gcloud iam policies get``). 10. The secret's own IAM policy gives read to one account, and not
to the intake, the control plane or anyone public (read-only ``gcloud secrets get-iam-policy``).
11 and 12 are **manual**: they need impersonation, which the kit never does.

No deployment record exposes a pinned version: ``live_version`` in ``secret list`` is the only
place (``routes/v1/secrets.py:98-103``). Pass: every automatic check passed; 11 and 12 never set
the verdict. The kit never prints or saves a value, a token or the cookie.
"""

import argparse
import hashlib
import json
import re
import secrets
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from proofrun.common import (
    KIT,
    REPO,
    CommandError,
    CookieError,
    CookieJar,
    Done,
    Fetched,
    Http,
    Outcome,
    Run,
    app_environment,
    app_service,
    fence,
    fetch,
    gcloud_json,
    host_of,
    results_dir,
    run_command,
    session_headers,
    ssc_error,
    ssc_prefix,
)

APP_FOLDER: Final = KIT / "apps" / "secrets"
NAME: Final = "GA46_TOKEN"
ENV: Final = "preview"
SET_TIMEOUT_S: Final = 900
DEPLOY_TIMEOUT_S: Final = 1200
POLL_GAP_S: Final = 10.0
POLL_LIMIT_S: Final = 600.0
READ_TRIES: Final = 6
READ_GAP_S: Final = 10.0
READ_TIMEOUT_S: Final = 90.0
FINGERPRINT_HEX: Final = 12
LOGIN_STATUSES: Final = (301, 302, 303, 307, 308)
ROW_KEYS: Final = frozenset({"name", "version", "live_version", "updated_at"})
DENY_POLICY: Final = "ssc-deny-secret-read"
DENIED_PERMISSION: Final = "secretmanager.googleapis.com/versions.access"
READ_ROLES: Final = frozenset(
    {
        "roles/secretmanager.secretAccessor",
        "roles/secretmanager.admin",
        "roles/owner",
        "roles/editor",
    }
)
PUBLIC_MEMBERS: Final = ("allUsers", "allAuthenticatedUsers")
NOTE_PIN: Final = (
    "note: a deployment's pinned versions are visible only as live_version in secret list; "
    "no deployment record exposes secret_refs"
)
LEAVES_BEHIND: Final = (
    f"leaves behind: the secret {NAME} in the app's preview with two or more versions "
    "(there is no `ssc secret delete`); each run adds two"
)
_COMMAND: Final = re.compile(r"(?m)^[\s│]*([a-z][a-z-]*)\s{2,}\S")
NAMES: Final = {
    1: f"[real] ssc secret set {NAME} v1 on stdin",
    2: "[real] v1 is live: the deployment is healthy",
    3: "the app answers v1's fingerprint and length through its host",
    4: "secret list shows name, version, live_version and no value or fingerprint",
    5: "no ssc secret command reads a value back (only set and list)",
    6: "[real] set v2 started its own deployment (rotation redeploys, no ssc deploy)",
    7: "right after, version is v2 and live_version still v1, and the app still answers v1",
    8: "[real] live_version becomes v2 and the app answers v2's fingerprint",
    9: "deny rule ssc-deny-secret-read denies versions.access to ssc-secret-intake",
    10: "the secret's IAM policy lets one account read it, not the intake or the control plane",
    11: "manual: the intake's account is refused a read and a create",
    12: "manual: the control plane's account is refused adding a version",
}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app", required=True, help="the secrets fixture app's slug")
    parser.add_argument("--label", required=True, help="the cell's label")
    parser.add_argument("--env", choices=(ENV,), default=ENV, help="preview only")
    parser.add_argument(
        "--control-sa", help="the control plane account the intake trusts (SSC_CONTROL_SA)"
    )


class RunInput(Protocol):
    """Runs one command with ``input`` on its stdin; the real one is :func:`run_input`."""

    def __call__(self, argv: Sequence[str], *, input: bytes, cwd: Path | None = None) -> Done: ...  # noqa: A002


def run_input(argv: Sequence[str], *, input: bytes, cwd: Path | None = None) -> Done:  # noqa: A002
    """Run ``argv`` with ``input`` on stdin, output captured, after fencing every argument."""
    fence(*argv)
    result = subprocess.run(  # noqa: S603
        list(argv), cwd=cwd, input=input, capture_output=True, check=False
    )
    return Done(
        result.returncode,
        result.stdout.decode(errors="replace"),
        result.stderr.decode(errors="replace"),
    )


@dataclass(frozen=True, slots=True)
class Check:
    """One numbered check; ``result`` None is "not read", and ``manual`` is for the person."""

    n: int
    name: str
    result: bool | None
    detail: str
    manual: bool = False

    @property
    def word(self) -> str:
        if self.manual:
            return "manual"
        return {True: "PASS", False: "FAIL", None: "not read"}[self.result]

    def line(self) -> str:
        return f"check {self.n} {self.word}: {self.name} ({self.detail})"

    def data(self) -> dict[str, Any]:
        return {"n": self.n, "name": self.name, "result": self.word, "detail": self.detail}


def fingerprint(value: str) -> tuple[str, int]:
    """What the app answers for ``value``: 12 hex of the SHA-256 of its bytes, and the length."""
    raw = value.encode()
    return hashlib.sha256(raw).hexdigest()[:FINGERPRINT_HEX], len(raw)


@dataclass
class State:
    run: Run
    runin: RunInput
    http: Http
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    slug: str
    label: str
    control_sa: str | None
    value1: str
    value2: str
    checks: dict[int, Check] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    v1: str | None = None
    v2: str | None = None
    op1: str | None = None
    env_id: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    problem: str | None = None

    @property
    def fp1(self) -> tuple[str, int]:
        return fingerprint(self.value1)

    @property
    def fp2(self) -> tuple[str, int]:
        return fingerprint(self.value2)

    def clean(self, text: str) -> str:
        """``text`` from a command or a host, with the values blanked in case one echoed them."""
        for value in (self.value1, self.value2):
            text = text.replace(value, "[value]")
        return text

    def add(self, check: Check) -> None:
        self.checks[check.n] = check

    def ssc(self, *args: str, value: str | None = None) -> Done:
        """Run ``ssc <args>``; ``value`` goes on stdin only. Whatever it printed is kept in
        memory for the leak scans, never shown."""
        argv = [*ssc_prefix(), *args]
        fence(*argv)
        if value is None:
            done = self.run(argv, cwd=REPO)
        else:
            done = self.runin(argv, input=value.encode(), cwd=REPO)
        self.outputs.append(done.stdout + "\n" + done.stderr)
        return done

    def secret_list(self, *extra: str) -> Done:
        return self.ssc("secret", "list", self.slug, "--env", ENV, *extra)

    def leaks(self) -> list[str]:
        """Which of the values and fingerprints appear in anything an ``ssc`` command printed."""
        text = "\n".join(self.outputs)
        wanted = {
            "value 1": self.value1,
            "value 2": self.value2,
            "fingerprint 1": self.fp1[0],
            "fingerprint 2": self.fp2[0],
        }
        return [label for label, secret in wanted.items() if secret in text]


def parse(done: Done) -> dict[str, Any] | None:
    """The JSON object a command printed on stdout, else None."""
    try:
        value = json.loads(done.stdout)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def secret_row(done: Done) -> dict[str, Any] | None:
    """The ``GA46_TOKEN`` row of ``secret list --json``."""
    rows = (parse(done) or {}).get("secrets")
    found = [r for r in rows or [] if isinstance(r, dict) and r.get("name") == NAME]
    return found[0] if found else None


def check_set(st: State, done: Done) -> Check:
    """Check 1: v1 stored, a numbered version, and the environment named."""
    data = parse(done)
    if done.returncode != 0 or data is None:
        return Check(1, NAMES[1], False, f"exit {done.returncode}: {st.clean(ssc_error(done))}")
    version = str(data.get("version"))
    ok = data.get("changed") is True and data.get("name") == NAME and version.isdigit()
    st.v1, st.op1, st.env_id = version, data.get("operation_id"), data.get("environment_id")
    return Check(
        1, NAMES[1], ok, f"version {version}, operation {st.op1}, state {data.get('state')}"
    )


def check_live(st: State, set_done: Done, deployed: Done | None) -> Check:
    """Check 2: the set's own deployment, else ``ssc deploy``, ended healthy."""
    source = set_done if deployed is None else deployed
    data = parse(source) or {}
    how = "the set's deployment" if deployed is None else "ssc deploy"
    ok = source.returncode == 0 and data.get("state") == "healthy"
    why = "" if ok else f": {st.clean(ssc_error(source))}"
    return Check(2, NAMES[2], ok, f"{how} {data.get('operation_id')} {data.get('state')}{why}")


def connect(st: State) -> None:
    """Find the app's host and its cookie. A reason is kept when either is missing."""
    try:
        url = str(app_environment(st.run, st.slug, ENV)["url"]).rstrip("/")
        fence(url)
        st.headers = session_headers(CookieJar().get(host_of(url)))
        st.url = url
    except (CommandError, CookieError, ValueError, KeyError) as exc:
        st.problem = st.clean(str(exc))


def read_app(st: State, tries: int = READ_TRIES) -> Fetched | None:
    """GET ``/secret`` with the cookie, retrying while the app wakes. A redirect (the login page)
    is not retried. The cookie goes to the app's host only."""
    if st.url is None or st.headers is None:
        return None
    got: Fetched | None = None
    for i in range(tries):
        got = st.http(st.url + "/secret", st.headers, READ_TIMEOUT_S)
        if got.status == 200 or got.status in LOGIN_STATUSES:
            return got
        if i + 1 < tries:
            st.sleep(READ_GAP_S)
    return got


def judge_answer(
    st: State, n: int, got: Fetched | None, value: str, want: tuple[str, int]
) -> Check:
    """Checks 3, 7 and 8: the app answered the fingerprint of ``value`` and nothing else."""
    name = NAMES[n]
    if got is None:
        return Check(n, name, None, st.problem or "the app was not reached")
    if got.status in LOGIN_STATUSES:
        host = host_of(st.url or "")
        hint = (
            f"the cookie is not accepted: log in again, then `python -m proofrun cookie set {host}`"
        )
        return Check(n, name, False, f"HTTP {got.status} (the login page); {hint}")
    if got.status != 200:
        return Check(n, name, False, f"HTTP {got.status} {got.error or ''}".strip())
    text = got.body.decode(errors="replace")
    if value in text:
        return Check(n, name, False, "the answer contains the value")
    try:
        data = json.loads(text)
    except ValueError:
        return Check(n, name, False, "the answer is not JSON")
    data = data if isinstance(data, dict) else {}
    ok = (
        data.get("name") == NAME
        and data.get("set") is True
        and data.get("fingerprint") == want[0]
        and data.get("length") == want[1]
    )
    seen = f"fingerprint {data.get('fingerprint')}, length {data.get('length')}"
    return Check(n, name, ok, f"{seen}; wanted {want[0]}, {want[1]}")


def check_list(st: State) -> Check:
    """Check 4: one row of exactly four fields, at v1 and live at v1, no value anywhere."""
    as_json = st.secret_list("--json")
    as_text = st.secret_list()
    row = secret_row(as_json)
    if as_json.returncode != 0 or as_text.returncode != 0 or row is None:
        return Check(4, NAMES[4], False, f"exit {as_json.returncode}/{as_text.returncode}: no row")
    leaked = st.leaks()
    ok = (
        set(row) == ROW_KEYS
        and row.get("version") == st.v1
        and row.get("live_version") == st.v1
        and NAME in as_text.stdout
        and not leaked
    )
    detail = f"fields {sorted(row)}, version {row.get('version')}, live {row.get('live_version')}"
    return Check(4, NAMES[4], ok, detail + (f", LEAKED {leaked}" if leaked else ", no leak"))


def check_help(done: Done) -> Check:
    """Check 5: the commands of ``ssc secret`` are ``set`` and ``list``."""
    found = sorted(set(_COMMAND.findall(done.stdout)))
    ok = done.returncode == 0 and found == ["list", "set"]
    return Check(5, NAMES[5], ok, f"commands {found}")


def check_rotation(st: State, done: Done) -> Check:
    """Check 6: v2 is a newer version and the route started a deployment by itself."""
    data = parse(done)
    if done.returncode != 0 or data is None:
        return Check(6, NAMES[6], False, f"exit {done.returncode}: {st.clean(ssc_error(done))}")
    version = str(data.get("version"))
    st.v2 = version
    newer = version.isdigit() and st.v1 is not None and int(version) > int(st.v1)
    ok = data.get("changed") is True and newer and bool(data.get("operation_id"))
    detail = f"version {version}, operation {data.get('operation_id')}, state {data.get('state')}"
    return Check(6, NAMES[6], ok, detail)


def check_pin(st: State) -> Check:
    """Check 7: stored v2, running v1; the app is asked once and still answers v1."""
    row = secret_row(st.secret_list("--json"))
    if row is None:
        return Check(7, NAMES[7], None, "secret list answered no row")
    seen = f"version {row.get('version')}, live_version {row.get('live_version')}"
    if row.get("live_version") == st.v2:
        return Check(7, NAMES[7], None, f"{seen}: the deployment had already landed")
    if row.get("version") != st.v2 or row.get("live_version") != st.v1:
        return Check(7, NAMES[7], False, seen)
    got = read_app(st, tries=1)
    if got is None or (got.status != 200 and got.status not in LOGIN_STATUSES):
        return Check(7, NAMES[7], None, f"{seen}; the app did not answer 200")
    answer = judge_answer(st, 7, got, st.value1, st.fp1)
    return Check(7, NAMES[7], answer.result, f"{seen}; {answer.detail}")


def wait_live(st: State) -> dict[str, Any] | None:
    """Poll ``secret list`` until ``live_version`` is v2, or the limit passes. Returns the row."""
    deadline = st.clock() + POLL_LIMIT_S
    while True:
        row = secret_row(st.secret_list("--json"))
        if row is not None and row.get("live_version") == st.v2:
            return row
        if st.clock() >= deadline:
            return row
        st.sleep(POLL_GAP_S)


def check_landed(st: State) -> Check:
    """Check 8: v2 live, the app answers it (and not v1), and no output held a value."""
    row = wait_live(st)
    if row is None or row.get("live_version") != st.v2:
        live = None if row is None else row.get("live_version")
        return Check(8, NAMES[8], False, f"live_version {live} after {POLL_LIMIT_S:.0f} s")
    answer = judge_answer(st, 8, read_app(st), st.value2, st.fp2)
    leaked = st.leaks()
    ok = answer.result is True and not leaked and st.fp1 != st.fp2
    detail = f"{answer.detail}; {'LEAKED ' + str(leaked) if leaked else 'no leak in any output'}"
    return Check(8, NAMES[8], ok if answer.result is not None else None, detail)


def intake_email(label: str) -> str:
    return f"ssc-secret-intake@ssc-c-{label}.iam.gserviceaccount.com"


def principal(email: str) -> str:
    return f"principal://iam.googleapis.com/projects/-/serviceAccounts/{email}"


def deny_command(label: str) -> list[str]:
    return [
        "iam", "policies", "get", DENY_POLICY,
        f"--attachment-point=cloudresourcemanager.googleapis.com/projects/ssc-c-{label}",
        "--kind=denypolicies",
    ]  # fmt: skip


def secret_command(label: str, secret: str) -> list[str]:
    return ["secrets", "get-iam-policy", secret, f"--project=ssc-c-{label}"]


def secret_name_of(env_id: str | None) -> str | None:
    try:
        return None if env_id is None else f"{app_service(env_id)}-{NAME}"
    except ValueError:
        return None


def judge_deny(st: State, policy: Any) -> Check:
    """Check 9: a rule with no condition denies ``versions.access`` to the intake."""
    mine = {principal(intake_email(st.label)), f"serviceAccount:{intake_email(st.label)}"}
    rules = [r.get("denyRule") or {} for r in (policy or {}).get("rules") or []]
    denied: set[str] = set()
    for rule in rules:
        permissions = {str(p).lower() for p in rule.get("deniedPermissions") or []}
        if DENIED_PERMISSION.lower() in permissions and not rule.get("denialCondition"):
            excepted = set(rule.get("exceptionPrincipals") or [])
            denied |= set(rule.get("deniedPrincipals") or []) - excepted
    ok = bool(mine & denied)
    named = {p.rsplit("/", 1)[-1].split("@")[0] for p in denied}
    control = "not given"
    if st.control_sa:
        control = (
            "named"
            if {principal(st.control_sa), f"serviceAccount:{st.control_sa}"} & denied
            else "not named"
        )
    detail = (
        f"unconditional deny of {DENIED_PERMISSION} to {sorted(named)}; intake "
        f"{'named' if ok else 'NOT named'}; control plane {control} (detail only: it holds no "
        "role in the cell, so absence from the rule is expected)"
    )
    return Check(9, NAMES[9], ok, detail)


def judge_secret_policy(st: State, policy: Any) -> Check:
    """Check 10: one reader, a service account, and neither the intake nor the control plane."""
    bindings = (policy or {}).get("bindings") or []
    members = {m for b in bindings for m in b.get("members") or []}
    readers = {m for b in bindings if b.get("role") in READ_ROLES for m in b.get("members") or []}
    barred = {f"serviceAccount:{intake_email(st.label)}"}
    if st.control_sa:
        barred.add(f"serviceAccount:{st.control_sa}")
    public = [m for m in members if m in PUBLIC_MEMBERS]
    ok = (
        len(readers) == 1
        and all(r.startswith("serviceAccount:") for r in readers)
        and not (barred & members)
        and not public
    )
    return Check(10, NAMES[10], ok, f"readers {sorted(readers)}; all members {sorted(members)}")


def policy_check(st: State, n: int, args: list[str], judge: Callable[[State, Any], Check]) -> Check:
    """A read-only gcloud check; unreadable is "not read" with the command to run by hand."""
    fence(*args)
    try:
        policy = gcloud_json(st.run, *args)
    except (CommandError, ValueError) as exc:
        by_hand = shlex.join(["gcloud", *args])
        return Check(n, NAMES[n], None, f"{st.clean(str(exc))}; run by hand: {by_hand}")
    return judge(st, policy)


def manual_lines(st: State) -> list[str]:
    """Checks 11 and 12 with the cell's names filled in. The kit never impersonates."""
    project = f"ssc-c-{st.label}"
    secret = secret_name_of(st.env_id) or f"ssc-a-<env id suffix>-{NAME}"
    intake = intake_email(st.label)
    control = st.control_sa or "<SSC_CONTROL_SA of the intake service>"
    read = ["gcloud", "secrets", "versions", "access", "latest", f"--secret={secret}"]
    read += [f"--project={project}", f"--impersonate-service-account={intake}"]
    create = ["gcloud", "secrets", "create", "ga46-probe", f"--project={project}"]
    create += ["--replication-policy=automatic", f"--impersonate-service-account={intake}"]
    add = ["gcloud", "secrets", "versions", "add", secret, "--data-file=-", f"--project={project}"]
    add += [f"--impersonate-service-account={control}"]
    lines = [
        "manual 11 and 12 (infra/README.md Secrets, live checks 2 and 3): need "
        "roles/iam.serviceAccountTokenCreator on the account named, for the operator, while "
        "they run "
        "(on 2026-10-08 the founder's account could not impersonate).",
        "do not run 11 or 12 while a deployment is pending.",
        f"check 11 (the intake cannot read back or create): {shlex.join(read)}",
        "  must end PERMISSION_DENIED. If it ever prints a value, that is a FAIL: rotate it.",
        f"  and {shlex.join(create)}",
        "  must be refused (the intake holds secretVersionAdder only). If it works, delete it.",
        f"check 12 (the control plane cannot add a version): printf x | {shlex.join(add)}",
        "  must be refused. WARNING: if it succeeds it adds a junk version, which the app would "
        "take at its next deployment: stop and tell the orchestrator.",
        "  if the SA is unknown, read it with: gcloud run services describe ssc-secret-intake "
        f"--project={project} --region=us-central1 "
        "--format='value(spec.template.spec.containers[0].env)'",
    ]
    return lines


def start_text(slug: str) -> list[str]:
    return [
        f"app {slug}: preview only (ssc deploy always targets preview, so --env prod is refused).",
        "[real] steps about to run, without asking:",
        f"  [real] 1. ssc secret set {NAME} v1, waiting for it to be live",
        "  [real] 2. ssc deploy of apps/secrets, only if nothing was live",
        f"  [real] 3. ssc secret set {NAME} v2: its own deployment, polled up to 10 minutes",
        "the values are random, held in memory and shown nowhere; only fingerprints are.",
    ]


def first_half(st: State) -> bool:
    """Checks 1 to 4. Returns whether v1 is stored and live."""
    set1 = st.ssc(
        "secret", "set", st.slug, NAME, "--env", ENV, "--wait", "--timeout", str(SET_TIMEOUT_S),
        "--json", value=st.value1,
    )  # fmt: skip
    st.add(check_set(st, set1))
    if st.checks[1].result is not True:
        return False
    deployed = None
    if st.op1 is None:
        deployed = st.ssc(
            "deploy", "--app", st.slug, str(APP_FOLDER), "--wait", "--timeout",
            str(DEPLOY_TIMEOUT_S), "--json",
        )  # fmt: skip
    st.add(check_live(st, set1, deployed))
    if st.checks[2].result is not True:
        return False
    connect(st)
    st.add(judge_answer(st, 3, read_app(st), st.value1, st.fp1))
    st.add(check_list(st))
    return True


def rotation(st: State) -> None:
    """Checks 6 to 8: v2, the pin observed before it lands, then the app on v2."""
    done = st.ssc(
        "secret", "set", st.slug, NAME, "--env", ENV, "--json", value=st.value2
    )  # fmt: skip
    st.add(check_rotation(st, done))
    if st.checks[6].result is not True:
        return
    st.add(check_pin(st))
    st.add(check_landed(st))


def finish(st: State) -> list[Check]:
    """Every numbered check; those not reached are "not read", 11 and 12 are manual."""
    for n, name in NAMES.items():
        if n in (11, 12):
            st.add(Check(n, name, None, "run the commands printed above", manual=True))
        elif n not in st.checks:
            st.add(Check(n, name, None, "not run: an earlier step did not finish"))
    return [st.checks[n] for n in sorted(st.checks)]


def verdict(checks: Sequence[Check]) -> bool | None:
    automatic = [c for c in checks if not c.manual]
    if any(c.result is False for c in automatic):
        return False
    return None if any(c.result is None for c in automatic) else True


def save(outcome: Outcome, app: str) -> None:
    """The per-run file beside the shared history; it holds no value, token or cookie."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = results_dir()
    folder.mkdir(parents=True, exist_ok=True)
    body = {"verdict": outcome.verdict, "number": outcome.number, "lines": outcome.lines}
    (folder / f"secrets-{app}-{stamp}.json").write_text(
        json.dumps({**body, **outcome.data}, indent=2, sort_keys=True) + "\n"
    )


def run(  # noqa: PLR0913, PLR0917  (every seam is injectable)
    args: argparse.Namespace,
    run: Run = run_command,
    runin: RunInput = run_input,
    http: Http = fetch,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    say: Callable[[str], None] = print,
    make_value: Callable[[], str] = lambda: secrets.token_urlsafe(32),
) -> Outcome:
    control = getattr(args, "control_sa", None)
    fence(args.app, args.label, control or "")
    st = State(
        run, runin, http, sleep, clock, args.app, args.label, control, make_value(), make_value()
    )
    plan = start_text(args.app)
    for line in plan:
        say(line)
    st.add(check_help(st.ssc("secret", "--help")))
    landed = first_half(st)
    if landed:
        rotation(st)
    st.add(policy_check(st, 9, deny_command(args.label), judge_deny))
    name = secret_name_of(st.env_id)
    if name is None:
        st.add(Check(10, NAMES[10], None, "the environment id is not known: set did not finish"))
    else:
        st.add(policy_check(st, 10, secret_command(args.label, name), judge_secret_policy))
    st.lines += [*manual_lines(st), NOTE_PIN, LEAVES_BEHIND]
    checks = finish(st)
    st.lines += [c.line() for c in checks]
    st.data.update(
        app=args.app,
        label=args.label,
        plan=plan,
        checks=[c.data() for c in checks],
        notes=[NOTE_PIN, LEAVES_BEHIND],
        v1=st.v1,
        v2=st.v2,
        fingerprint_1=st.fp1[0],
        length_1=st.fp1[1],
        fingerprint_2=st.fp2[0],
        length_2=st.fp2[1],
    )
    automatic = [c for c in checks if not c.manual]
    passed = sum(c.result is True for c in automatic)
    number = (
        f"v{st.v1}/v{st.v2}, {passed} of {len(automatic)} automatic checks passed, 11 and 12 manual"
    )
    outcome = Outcome("GA-4.6", number, verdict(checks), st.lines, st.data)
    save(outcome, args.app)
    return outcome
