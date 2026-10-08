"""GA-4.5: the rollback warning (``409 SCHEMA_AHEAD``) seen in the CLI, MCP and console, and the
``--confirm`` path audited.

``rollback --app <slug> [--console-url https://console.delimitus.com]``

The app is ``apps/rollback`` (``[state] postgres = true`` and an alembic ledger the platform reads
from the source and never runs). Only preview is used: ``ssc deploy`` always targets preview.
The kit makes two releases from the one folder: R1 from a temporary copy without
``alembic/versions/0002_ga45_second.py``, R2 from the whole folder. The four **[real]** steps
create deployments on the real cell; each one waits for the deployment to be live.

1. **[real]** deploy R1 (``--wait``). 2. **[real]** deploy R2 (``--wait``).
3. ``ssc rollback <slug> R1`` is refused: ``Code: SCHEMA_AHEAD`` and the migration's name in the
   ``Fix:`` line. 4. The same with ``--json``: the code only; the names are text-only
   (``CliError.fix``), which the output and the results say.
5. ``ssc mcp`` over stdio (newline-delimited JSON-RPC): ``initialize``, ``tools/list``, then the
   ``rollback`` tool, which must be refused with ``SCHEMA_AHEAD`` naming the migration.
   It needs an agent's login; without one the check is "not read" and the command is printed.
6. **[real]** ``ssc rollback <slug> R1 --confirm --wait``. 7. It reaches healthy on R1.
8. ``ssc audit export`` holds one ``rollback.started`` row for R1 on this environment (the refusals
   wrote none), with ``confirmed`` and ``migrations_ahead`` naming the migration.
9. **[real]** ``ssc rollback <slug> R2 --wait`` needs no ``--confirm`` and puts R2 back, so that
   R1 can be picked in the console. 10. The console is a browser page: the kit prints the URL and
   what the person must see, and the check stays "manual".

Pass: every automatic check passed. The kit never reads the operator's token.
"""

import argparse
import json
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Final, Protocol

from proofrun.common import (
    KIT,
    REPO,
    Done,
    Outcome,
    Run,
    fence,
    last_line,
    results_dir,
    run_command,
    ssc_error,
    ssc_prefix,
)

APP_FOLDER: Final = KIT / "apps" / "rollback"
AHEAD_FILE: Final = "alembic/versions/0002_ga45_second.py"
AHEAD: Final = "0002_ga45_second"
FIRST: Final = "0001_ga45_first"
LEDGER_ENTRY: Final = f"alembic:{AHEAD}"
AGENT: Final = "ga45"
CONSOLE_URL: Final = "https://console.delimitus.com"
FIRST_TIMEOUT_S: Final = 1200
TIMEOUT_S: Final = 900
MCP_LIMIT_S: Final = 60.0
SINCE_MARGIN_S: Final = 120
ACTION: Final = "rollback.started"
NOTE_JSON: Final = "note: names absent from --json (CliError.fix is text-only, errors.py:104-106)"
PREREQUISITE: Final = (
    f"ssc mcp needs an agent's login: run `ssc login --org <org id> --agent {AGENT}` "
    "(or set SSC_TOKEN to an agent's token), then run this again"
)
NAMES: Final = {
    1: "R1 (0001 only) deployed and healthy",
    2: "R2 (0001 and 0002) deployed and healthy",
    3: f"CLI refuses the rollback to R1: SCHEMA_AHEAD naming {AHEAD}",
    4: "CLI --json refusal carries SCHEMA_AHEAD",
    5: f"MCP rollback tool refuses with SCHEMA_AHEAD naming {AHEAD}",
    6: "the refusals started nothing: one rollback.started row for R1, the confirmed one",
    7: "ssc rollback --confirm to R1 reaches healthy",
    8: f"its audit row says confirmed and migrations_ahead {LEDGER_ENTRY}",
    9: "rollback to R2 needs no --confirm and reaches healthy",
    10: "console shows the warning with the migration's name",
}
REAL_STEPS: Final = (
    "deploy R1 (0001 only) to preview and wait for healthy",
    "deploy R2 (0001 and 0002) to preview and wait for healthy",
    "ssc rollback to R1 with --confirm, waiting for healthy",
    "ssc rollback to R2 (no --confirm), waiting for healthy",
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app", required=True, help="the rollback app's slug (ssc apps create)")
    parser.add_argument("--console-url", default=CONSOLE_URL, help="where the console is served")


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


@dataclass(frozen=True, slots=True)
class Release:
    """A deployed release, from ``ssc deploy --json``."""

    number: int
    release_id: str
    app_id: str
    environment_id: str


class McpConnection(Protocol):
    """One ``ssc mcp`` child: requests answered by id, or None when nothing came back."""

    def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any] | None: ...

    def notify(self, method: str) -> None: ...

    def close(self) -> str:
        """Stop the child and return what it wrote on stderr."""
        ...


type OpenMcp = Callable[[Sequence[str]], McpConnection]


class PipeMcp:
    """The real connection: newline-delimited JSON on the child's stdin and stdout, a 60 s limit
    for the whole conversation, and a child that is always stopped."""

    def __init__(self, argv: Sequence[str], limit: float = MCP_LIMIT_S) -> None:
        fence(*argv)
        self.deadline = time.monotonic() + limit
        self.proc = subprocess.Popen(  # noqa: S603
            list(argv),
            cwd=REPO,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.errors: list[str] = []
        self.next_id = 0
        self.threads = [
            threading.Thread(target=self._pump, args=(self.proc.stdout,), daemon=True),
            threading.Thread(target=self._drain, args=(self.proc.stderr,), daemon=True),
        ]
        for t in self.threads:
            t.start()

    def _pump(self, stream: IO[str] | None) -> None:
        for line in stream or ():
            self.lines.put(line)
        self.lines.put(None)

    def _drain(self, stream: IO[str] | None) -> None:
        self.errors.extend(stream or ())

    def _send(self, message: Mapping[str, Any]) -> bool:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except OSError, ValueError:
            return False
        return True

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
        self.next_id += 1
        mine = self.next_id
        if not self._send({"jsonrpc": "2.0", "id": mine, "method": method, "params": params}):
            return None
        while True:
            left = self.deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == mine:
                return message

    def close(self) -> str:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for t in self.threads:
            t.join(2)
        return "".join(self.errors)


@dataclass
class McpFindings:
    """What the MCP conversation showed."""

    initialized: bool = False
    tools: list[str] | None = None
    call: dict[str, Any] | None = None
    stderr: str = ""


@dataclass
class State:
    run: Run
    open_mcp: OpenMcp
    say: Callable[[str], None]
    slug: str
    console_url: str
    checks: dict[int, Check] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    r1: Release | None = None
    r2: Release | None = None

    def ssc(self, *args: str) -> Done:
        argv = [*ssc_prefix(), *args]
        fence(*argv)
        return self.run(argv, cwd=REPO)

    def add(self, check: Check) -> None:
        self.checks[check.n] = check


def parse(done: Done) -> dict[str, Any] | None:
    """The JSON object a command printed on stdout, else None."""
    try:
        value = json.loads(done.stdout)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def release_of(data: Mapping[str, Any]) -> Release:
    return Release(
        int(data["release_number"]),
        str(data["release_id"]),
        str(data["app_id"]),
        str(data["environment_id"]),
    )


def check_deployed(n: int, done: Done, above: int | None = None) -> Check:
    """Check 1 or 2: ``ssc deploy --wait --json`` ended healthy (and past release ``above``)."""
    data = parse(done)
    if done.returncode != 0 or data is None:
        return Check(n, NAMES[n], False, f"exit {done.returncode}: {ssc_error(done)}")
    number, state = data.get("release_number"), data.get("state")
    ok = state == "healthy" and isinstance(number, int) and (above is None or number > above)
    return Check(n, NAMES[n], ok, f"R{number} {state}")


def check_cli_text(done: Done) -> Check:
    """Check 3: refused, the code and the migration's name shown, 0001 not named as ahead."""
    shown = done.stderr + done.stdout
    ok = done.returncode != 0 and "SCHEMA_AHEAD" in shown and AHEAD in shown and FIRST not in shown
    return Check(3, NAMES[3], ok, f"exit {done.returncode}; {last_line(done.stderr)[:160]}")


def check_cli_json(done: Done) -> Check:
    """Check 4: refused with the code in ``error.code``."""
    data = parse(done)
    error = (data or {}).get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return Check(
        4,
        NAMES[4],
        done.returncode != 0 and code == "SCHEMA_AHEAD",
        f"exit {done.returncode}, code {code}",
    )


def names_in_json(done: Done) -> bool:
    return AHEAD in done.stdout


def check_mcp(found: McpFindings) -> Check:
    """Check 5: the ``rollback`` tool is refused with the code and the name."""
    if not found.initialized:
        if "AGENT_TOKEN_REQUIRED" in found.stderr:
            return Check(5, NAMES[5], None, PREREQUISITE)
        why = last_line(found.stderr) or "no output"
        return Check(5, NAMES[5], None, f"ssc mcp did not answer initialize: {why}")
    if found.tools is None or found.call is None:
        return Check(5, NAMES[5], None, "no answer to tools/list or tools/call")
    if "rollback" not in found.tools:
        return Check(5, NAMES[5], False, f"tools/list has no rollback: {', '.join(found.tools)}")
    result = found.call.get("result")
    if not isinstance(result, dict):
        return Check(5, NAMES[5], False, f"tools/call answered {json.dumps(found.call)[:160]}")
    error = (result.get("structuredContent") or {}).get("error") or {}
    text = " ".join(str(c.get("text", "")) for c in result.get("content") or [])
    ok = result.get("isError") is True and error.get("code") == "SCHEMA_AHEAD"
    ok = ok and AHEAD in text and AHEAD in str(error.get("detail"))
    return Check(5, NAMES[5], ok, f"isError {result.get('isError')}, code {error.get('code')}")


def audit_rows(text: str) -> list[dict[str, Any]]:
    """The audit rows of a JSON-lines export; a line that is not a row is skipped."""
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "action" in row:
            rows.append(row)
    return rows


def started_for(rows: Sequence[Mapping[str, Any]], r1: Release) -> list[Mapping[str, Any]]:
    """The ``rollback.started`` rows for R1 on this environment."""
    found = []
    for row in rows:
        after = row.get("after") or {}
        if row.get("action") == ACTION and after.get("release_id") == r1.release_id:
            if after.get("environment_id") == r1.environment_id:
                found.append(row)
    return found


def check_untouched(rows: Sequence[Mapping[str, Any]], operation_id: str) -> Check:
    ids = [(r.get("target") or {}).get("id") for r in rows]
    ok = ids == [operation_id]
    return Check(6, NAMES[6], ok, f"{len(rows)} rollback.started rows for R1: {ids}")


def check_audit_row(rows: Sequence[Mapping[str, Any]], operation_id: str) -> Check:
    row = next((r for r in rows if (r.get("target") or {}).get("id") == operation_id), None)
    if row is None:
        return Check(8, NAMES[8], False, f"no {ACTION} row for {operation_id}")
    after = row.get("after") or {}
    ahead = after.get("migrations_ahead")
    ok = after.get("confirmed") is True and isinstance(ahead, list) and LEDGER_ENTRY in ahead
    actor = row.get("actor") or {}
    detail = (
        f"{row.get('action')} by {actor.get('kind')} {actor.get('id')} "
        f"via_agent {actor.get('via_agent')}, confirmed {after.get('confirmed')}, "
        f"migrations_ahead {ahead}"
    )
    return Check(8, NAMES[8], ok, detail)


def check_rolled(n: int, done: Done, want: Release | None) -> Check:
    """Check 7 or 9: ``ssc rollback --wait --json`` ended healthy on the wanted release."""
    data = parse(done)
    if done.returncode != 0 or data is None or want is None:
        return Check(n, NAMES[n], False, f"exit {done.returncode}: {ssc_error(done)}")
    ok = data.get("state") == "healthy" and data.get("release_number") == want.number
    return Check(n, NAMES[n], ok, f"R{data.get('release_number')} {data.get('state')}")


def verdict(checks: Sequence[Check]) -> bool | None:
    automatic = [c for c in checks if not c.manual]
    if any(c.result is False for c in automatic):
        return False
    return None if any(c.result is None for c in automatic) else True


def console_lines(url: str, slug: str, app_id: str | None, r1: Release | None) -> list[str]:
    label = f"R{r1.number}" if r1 else "R1"
    return [
        f"console (manual): open {url.rstrip('/')}/apps/{app_id or '<app id from ssc status>'}",
        f"  Preview card, Roll back, pick {label}, type {slug}, press Roll back.",
        "  Do not tick the checkbox: press Cancel after looking.",
        f"  must see: 'The database may have run migrations {label} does not have' and a list "
        f"item {AHEAD} (alembic), with the box unticked and the button 'Roll back anyway' off.",
        "  record by hand what the page showed.",
    ]


def start_text(slug: str) -> list[str]:
    return [
        f"app {slug}: preview only (ssc deploy always targets preview, so there is no --env).",
        "[real] steps about to run, without asking:",
        *[f"  [real] {i}. {s}" for i, s in enumerate(REAL_STEPS, 1)],
    ]


def deploy(st: State, folder: Path, timeout: int) -> Done:
    return st.ssc(
        "deploy", "--app", st.slug, str(folder), "--wait", "--timeout", str(timeout), "--json"
    )


def deploy_both(st: State) -> bool:
    """Steps 1 and 2. R1 is deployed from a temporary copy without the second migration."""
    temp = Path(tempfile.mkdtemp(prefix="ga45-"))
    try:
        copy = temp / "app"
        shutil.copytree(APP_FOLDER, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (copy / AHEAD_FILE).unlink()
        first = deploy(st, copy, FIRST_TIMEOUT_S)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    st.add(check_deployed(1, first))
    if st.checks[1].result is not True:
        return False
    st.r1 = release_of(parse(first) or {})
    second = deploy(st, APP_FOLDER, TIMEOUT_S)
    st.add(check_deployed(2, second, above=st.r1.number))
    if st.checks[2].result is not True:
        return False
    st.r2 = release_of(parse(second) or {})
    return True


def refusals(st: State, r1: Release) -> None:
    """Steps 3 and 4: the CLI without and with ``--json``."""
    ref = f"R{r1.number}"
    text = st.ssc("rollback", st.slug, ref, "--env", "preview")
    st.add(check_cli_text(text))
    st.lines.append(f"CLI text refusal: exit {text.returncode}, {last_line(text.stderr)}")
    as_json = st.ssc("rollback", st.slug, ref, "--env", "preview", "--json")
    st.add(check_cli_json(as_json))
    named = names_in_json(as_json)
    note = f"note: names present in --json ({AHEAD})" if named else NOTE_JSON
    st.lines.append(note)
    st.data.update(cli_json_names=named, notes=[note])


def mcp_conversation(open_mcp: OpenMcp, slug: str, release_id: str) -> McpFindings:
    """Step 5: ``initialize``, ``tools/list``, then ``rollback`` without ``confirm``."""
    found = McpFindings()
    conn = open_mcp([*ssc_prefix(), "mcp"])
    try:
        hello = conn.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "proofrun", "version": "0.0.1"},
            },
        )
        found.initialized = bool(hello and "result" in hello)
        if found.initialized:
            conn.notify("notifications/initialized")
            listed = conn.request("tools/list", {})
            tools = ((listed or {}).get("result") or {}).get("tools")
            found.tools = [str(t.get("name")) for t in tools] if isinstance(tools, list) else None
            arguments = {"app": slug, "release": release_id, "env": "preview"}
            found.call = conn.request("tools/call", {"name": "rollback", "arguments": arguments})
    finally:
        found.stderr = conn.close()
    return found


def mcp_step(st: State, r1: Release) -> None:
    found = mcp_conversation(st.open_mcp, st.slug, r1.release_id)
    check = check_mcp(found)
    st.add(check)
    if check.result is None:
        st.lines.append(check.detail)
    st.data["mcp"] = {"tools": found.tools, "call": found.call}


def confirmed(st: State, r1: Release) -> str | None:
    """Step 5 of the rollbacks: ``--confirm``. Returns its operation id when it started."""
    done = st.ssc(
        "rollback", st.slug, f"R{r1.number}", "--env", "preview", "--confirm",
        "--wait", "--timeout", str(TIMEOUT_S), "--json",
    )  # fmt: skip
    st.add(check_rolled(7, done, r1))
    operation = (parse(done) or {}).get("operation_id")
    return str(operation) if operation else None


def export_audit(st: State, since: str, out: Path) -> Done:
    return st.ssc(
        "audit", "export", "--since", since, "--out", str(out), "--format", "jsonl", "--json"
    )


def audit_step(st: State, r1: Release, operation_id: str | None, since: str) -> None:
    """Checks 6 and 8: the export holds the confirmed rollback's row, and no other for R1."""
    if operation_id is None:
        why = "the confirmed rollback did not start"
        st.add(Check(6, NAMES[6], None, why))
        st.add(Check(8, NAMES[8], None, why))
        return
    temp = Path(tempfile.mkdtemp(prefix="ga45-audit-"))
    try:
        path = temp / "audit.jsonl"
        done = export_audit(st, since, path)
        text = path.read_text(encoding="utf-8") if done.returncode == 0 and path.exists() else None
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    if text is None:
        why = f"ssc audit export failed: {ssc_error(done)}"
        st.add(Check(6, NAMES[6], None, why))
        st.add(Check(8, NAMES[8], None, why))
        return
    rows = started_for(audit_rows(text), r1)
    st.add(check_untouched(rows, operation_id))
    st.add(check_audit_row(rows, operation_id))
    st.data["audit_rows"] = [
        {k: r.get(k) for k in ("seq", "at", "action", "actor", "target", "after")} for r in rows
    ]


def forward(st: State, r2: Release) -> None:
    """Step 9: back to R2, which needs no ``--confirm``."""
    done = st.ssc(
        "rollback", st.slug, f"R{r2.number}", "--env", "preview",
        "--wait", "--timeout", str(TIMEOUT_S), "--json",
    )  # fmt: skip
    st.add(check_rolled(9, done, r2))


def finish(st: State) -> list[Check]:
    """Every numbered check, those not reached as "not read"."""
    for n, name in NAMES.items():
        if n == 10:  # noqa: PLR2004
            st.add(Check(n, name, None, "open the page and look (see above)", manual=True))
        elif n not in st.checks:
            st.add(Check(n, name, None, "not run: an earlier step did not finish"))
    return [st.checks[n] for n in sorted(st.checks)]


def save(outcome: Outcome, app: str) -> None:
    """The per-run file beside the shared history; it holds no token."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = results_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"rollback-{app}-{stamp}.json").write_text(
        json.dumps(
            {
                "verdict": outcome.verdict,
                "number": outcome.number,
                "lines": outcome.lines,
                **outcome.data,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def number_of(checks: Sequence[Check], st: State) -> str:
    automatic = [c for c in checks if not c.manual]
    passed = sum(c.result is True for c in automatic)
    releases = f"R{st.r1.number}/R{st.r2.number}" if st.r1 and st.r2 else "no releases"
    return f"{releases}, {passed} of {len(automatic)} automatic checks passed, console manual"


def run(
    args: argparse.Namespace,
    run: Run = run_command,
    open_mcp: OpenMcp = PipeMcp,
    say: Callable[[str], None] = print,
    wall: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Outcome:
    st = State(run, open_mcp, say, args.app, args.console_url)
    plan = start_text(args.app)
    st.data.update(app=args.app, plan=plan)
    for line in plan:
        say(line)
    since = (wall() - timedelta(seconds=SINCE_MARGIN_S)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if deploy_both(st) and st.r1 is not None:
        refusals(st, st.r1)
        mcp_step(st, st.r1)
        operation = confirmed(st, st.r1)
        audit_step(st, st.r1, operation, since)
    if st.r2 is not None:
        forward(st, st.r2)
    app_id = st.r1.app_id if st.r1 else None
    st.lines += console_lines(args.console_url, args.app, app_id, st.r1)
    checks = finish(st)
    st.lines += [c.line() for c in checks]
    st.data["checks"] = [c.data() for c in checks]
    st.data.update(r1=st.r1.number if st.r1 else None, r2=st.r2.number if st.r2 else None)
    outcome = Outcome("GA-4.5", number_of(checks, st), verdict(checks), st.lines, st.data)
    save(outcome, args.app)
    return outcome
