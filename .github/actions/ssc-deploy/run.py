"""Run ``ssc deploy`` for the ssc-deploy Action and hand its result to the workflow.

The Action's step passes everything through the environment. ``SSC_TOKEN`` and ``SSC_API_URL``
reach ``ssc`` unchanged; ``SSC_BIN``, ``SSC_APP``, ``SSC_PATH`` and ``SSC_COMMIT`` say what to
run. The result goes to ``$GITHUB_OUTPUT`` (``preview-url``, ``release-id``, ``operation-id``),
and ``Preview: <url>`` goes to the log and ``$GITHUB_STEP_SUMMARY``. A failure becomes one
``::error::`` annotation, and the step exits with ``ssc``'s own exit code.

The runner reads a log line starting with ``::`` as a workflow command, so nothing ``ssc`` or the
API says starts a line: ``ssc``'s stderr is passed on behind ``ssc: ``, API text is kept to one
line, and text inside a command is escaped.
"""

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, TextIO

STDERR_PREFIX: Final = "ssc: "
OUTPUTS: Final = {"preview-url": "url", "release-id": "release_id", "operation-id": "operation_id"}
REQUIRED: Final = ("release_id", "operation_id")
EMPTY: Final = {
    "SSC_TOKEN": "The token input is empty. Workflows run for a fork get no secrets.",
    "SSC_APP": "The app input is empty.",
}
FAILED: Final = 1
USAGE: Final = 2


def escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    return escape_data(text).replace(":", "%3A").replace(",", "%2C")


def command(name: str, message: str, **properties: str) -> str:
    """A workflow command line, such as ``::error title=...::message``."""
    props = ",".join(f"{k}={escape_property(v)}" for k, v in properties.items())
    return f"::{name}{' ' if props else ''}{props}::{escape_data(message)}"


def emit(out: TextIO, line: str) -> None:
    out.write(line + "\n")


def one_line(text: object) -> str:
    return " ".join(str(text).splitlines())


def deploy_argv(env: Mapping[str, str]) -> list[str]:
    """``ssc deploy``'s argv. Values are joined to their options, so none is read as an option."""
    argv = [env.get("SSC_BIN") or "ssc", "deploy", f"--app={env['SSC_APP']}", "--json", "--wait"]
    if commit := env.get("SSC_COMMIT", ""):
        argv.append(f"--commit={commit}")
    return [*argv, "--", env.get("SSC_PATH") or "."]


def failure(doc: object, code: int) -> str:
    """What the annotation says when ``ssc deploy`` fails: its error, else its exit code."""
    error = doc.get("error") if isinstance(doc, dict) else None
    if not isinstance(error, dict):
        return f"ssc deploy exited {code}; its messages are above."
    text = f"{error.get('code')}: {error.get('title')} {error.get('detail')}"
    if error.get("request_id"):
        text += f" (request {error['request_id']})"
    return one_line(text)


def write_outputs(path: str, values: Mapping[str, str]) -> None:
    """Append to ``$GITHUB_OUTPUT`` with a random delimiter, so no value can end its entry."""
    with Path(path).open("a", encoding="utf-8") as f:
        for name, value in values.items():
            delimiter = f"ghadelimiter_{uuid.uuid4()}"
            f.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def summarize(result: Mapping[str, Any], out: TextIO) -> list[str]:
    """Log the deploy and return the step summary's lines."""
    for w in result.get("warnings") or []:
        where = f"{w.get('path')}:{w.get('line')}"
        message = f"{where} looks like a secret ({w.get('rule')}: {w.get('masked')}), not blocking."
        emit(out, command("warning", one_line(message), title="ssc secret scan"))
    for c in result.get("capability_changes") or []:
        emit(out, f"Change: {one_line(c.get('consequence'))}")
    number, release, op = result.get("release_number"), result["release_id"], result["operation_id"]
    line = f"R{number} ({release}) of {result.get('slug')} is {result.get('state')} in preview."
    emit(out, one_line(line))
    summary = [one_line(line), f"Operation: {op}"]
    url = result.get("url")
    if url:
        emit(out, f"Preview: {one_line(url)}")
        summary.insert(0, f"Preview: {one_line(url)}")
    else:
        emit(out, command("warning", "The API gave no preview URL for this app."))
    return summary


def main(env: Mapping[str, str] = os.environ, out: TextIO = sys.stdout) -> int:
    for key, message in EMPTY.items():
        if not env.get(key, "").strip():
            emit(out, command("error", message, title="ssc deploy"))
            return USAGE
    emit(out, command("add-mask", env["SSC_TOKEN"]))
    argv = deploy_argv(env)
    emit(out, one_line(f"Deploying {argv[-1]} to preview of {env['SSC_APP']}."))
    try:
        # A list, no shell: argv[0] is the ssc the Action installed, the rest are its arguments.
        ran = subprocess.run(argv, capture_output=True, text=True, env=dict(env), check=False)  # noqa: S603
    except OSError as e:
        emit(out, command("error", f"Could not run {argv[0]}: {e}", title="ssc deploy"))
        return FAILED
    for line in ran.stderr.splitlines():
        emit(out, STDERR_PREFIX + line)
    try:
        doc = json.loads(ran.stdout)
    except ValueError:
        doc = None
    if ran.returncode != 0:
        emit(out, command("error", failure(doc, ran.returncode), title="ssc deploy failed"))
        return ran.returncode if ran.returncode > 0 else FAILED
    if not isinstance(doc, dict) or not all(isinstance(doc.get(k), str) for k in REQUIRED):
        emit(out, command("error", "ssc deploy printed no deploy result.", title="ssc deploy"))
        return FAILED
    summary = summarize(doc, out)
    values = {name: one_line(doc.get(key) or "") for name, key in OUTPUTS.items()}
    if path := env.get("GITHUB_OUTPUT"):
        write_outputs(path, values)
    if path := env.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as f:
            f.write("### ssc deploy\n\n" + "".join(f"{s}  \n" for s in summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
