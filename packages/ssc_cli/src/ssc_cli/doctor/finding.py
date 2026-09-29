"""What ``ssc doctor`` reports: stable codes, their severity, and one fix text per code.

Codes and severities are part of decision 017; the set of codes may only grow. Fix texts name
only features that exist or are agreed in a decision (the manifest is decision 013).
"""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

Severity = Literal["block", "warn"]
DoctorCode = Literal[
    "LOCKFILE_STALE",
    "PUBLIC_ENV_AT_BUILD",
    "NO_START_COMMAND",
    "PORT_BINDING",
    "EXTERNAL_SERVICE",
    "WRITES_HOME",
    "NOT_SINGLE_APP",
    "MANIFEST_MISSING",
    "MANIFEST_INVALID",
]

LOCKFILE_STALE: Final = "LOCKFILE_STALE"
PUBLIC_ENV_AT_BUILD: Final = "PUBLIC_ENV_AT_BUILD"
NO_START_COMMAND: Final = "NO_START_COMMAND"
PORT_BINDING: Final = "PORT_BINDING"
EXTERNAL_SERVICE: Final = "EXTERNAL_SERVICE"
WRITES_HOME: Final = "WRITES_HOME"
NOT_SINGLE_APP: Final = "NOT_SINGLE_APP"
MANIFEST_MISSING: Final = "MANIFEST_MISSING"
MANIFEST_INVALID: Final = "MANIFEST_INVALID"

SEVERITY: Final[dict[DoctorCode, Severity]] = {
    LOCKFILE_STALE: "block",
    PUBLIC_ENV_AT_BUILD: "warn",
    NO_START_COMMAND: "block",
    PORT_BINDING: "block",
    EXTERNAL_SERVICE: "warn",
    WRITES_HOME: "warn",
    NOT_SINGLE_APP: "block",
    MANIFEST_MISSING: "warn",
    MANIFEST_INVALID: "block",
}

FIX: Final[dict[DoctorCode, str]] = {
    LOCKFILE_STALE: (
        "Regenerate the lock file with the tool it belongs to (npm install, bun install, "
        "pnpm install, yarn, poetry lock or uv lock), delete lock files left by other tools, "
        "and commit the result. The build installs with the lock file frozen."
    ),
    PUBLIC_ENV_AT_BUILD: (
        "Give the value for each environment under [build.public_env.preview] and "
        "[build.public_env.prod] in ssc.toml; a name that does not start with VITE_ or "
        "NEXT_PUBLIC_ must also be listed in public_names under [build]. The value is baked "
        "into the page at build time and anyone who can open the app can read it, so never put "
        "a secret there."
    ),
    NO_START_COMMAND: (
        "Add a start script to package.json, or a Procfile with one line such as "
        "`web: streamlit run app.py --server.port $PORT --server.address 0.0.0.0`, "
        "or set start under [runtime] in ssc.toml."
    ),
    PORT_BINDING: (
        "Listen on 0.0.0.0 and take the port from the PORT environment variable, for example "
        'app.run(host="0.0.0.0", port=int(os.environ["PORT"])) or '
        "app.listen(process.env.PORT)."
    ),
    EXTERNAL_SERVICE: (
        "SSC does not provide this service. For data, add [state] with postgres = true to "
        "ssc.toml and connect with DATABASE_URL. For sign-in, remove the vendor login and read "
        "the person from the identity note (ssc_app.identity or @delimitus/ssc-identity)."
    ),
    WRITES_HOME: (
        "Write scratch files under /tmp and keep lasting data in Postgres. Do not rely on a home "
        "folder: the app runs as a non-root user, and what it writes is lost on restart."
    ),
    NOT_SINGLE_APP: (
        "Run `ssc doctor` on the folder that holds one app, for example `ssc doctor ./backend`. "
        "SSC builds from source, so a folder of prebuilt files has nothing to build."
    ),
    MANIFEST_MISSING: (
        "Run `ssc init` to write a starter ssc.toml, then adjust it and commit it. Without one "
        "the app gets the defaults, which include no database."
    ),
    MANIFEST_INVALID: (
        'Correct each line listed. ssc.toml must start with schema = "ssc/v1", and the format '
        "is strict: an unknown key or a value of the wrong type is refused, never guessed."
    ),
}


class Finding(BaseModel):
    """One problem. ``path`` is relative to the checked folder, ``"."`` for the folder."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: DoctorCode
    severity: Severity
    path: str
    line: int | None
    message: str
    fix: str


def finding(code: DoctorCode, path: str, message: str, line: int | None = None) -> Finding:
    return Finding(
        code=code, severity=SEVERITY[code], path=path, line=line, message=message, fix=FIX[code]
    )
