"""Build failure codes and the fix-it shown for each (SSC-015).

A build ends with a release or one of these codes on the build row. The CLI shows the fix-it;
the code is stable, the text may change. The first four are Delimitus' build reasons, so its
deploy fixtures keep their expected codes.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from ssc_contracts.packages import HOW_TO_ASK

BUILD_DEPENDENCY_UNRESOLVED: Final = "BUILD_DEPENDENCY_UNRESOLVED"
BUILD_PRIVATE_REGISTRY: Final = "BUILD_PRIVATE_REGISTRY"
BUILD_NO_ENTRYPOINT: Final = "BUILD_NO_ENTRYPOINT"
BUILD_EXITED_NONZERO: Final = "BUILD_EXITED_NONZERO"
BUILD_UNSUPPORTED_RUNTIME: Final = "BUILD_UNSUPPORTED_RUNTIME"
SECRET_IN_BUNDLE: Final = "SECRET_IN_BUNDLE"  # noqa: S105  (a reason code, not a secret)
STATE_SQLITE_EPHEMERAL: Final = "STATE_SQLITE_EPHEMERAL"
ADD_APPROVED_PACKAGE: Final = "ADD_APPROVED_PACKAGE"

SQLITE_ON_DISK: Final = (
    "The app keeps SQLite on disk (STATE_SQLITE_EPHEMERAL). The file system is memory and is lost "
    "when the instance stops, so the build is refused. Add\n\n[state]\npostgres = true\n\nto "
    "ssc.toml and keep the data in Postgres; the app gets DATABASE_URL."
)

FIX_ITS: Final[Mapping[str, str]] = MappingProxyType(
    {
        BUILD_DEPENDENCY_UNRESOLVED: "A dependency could not be installed: a name or version "
        "that does not exist, or a lock file that no longer matches the dependency list. Fix the "
        "name, or run the install locally (npm install, bun install, poetry lock, uv lock), commit "
        "the updated lock file and deploy again.",
        BUILD_PRIVATE_REGISTRY: "The app installs from a private package registry, which the "
        "build cannot reach and holds no credentials for. Use public packages only; for one the "
        "platform does not carry, ask for it with ADD_APPROVED_PACKAGE.",
        BUILD_NO_ENTRYPOINT: "The build could not tell how to start the app. Set start under "
        "[runtime] in ssc.toml (for Streamlit: streamlit run app.py --server.port $PORT "
        "--server.address 0.0.0.0) or add a Procfile with one web: line. A notebook is not an "
        "app: move its code into a script that serves a page.",
        BUILD_EXITED_NONZERO: "A build step failed. Run `ssc doctor`, check that the app builds "
        "locally, and deploy again.",
        BUILD_UNSUPPORTED_RUNTIME: "This kind of app is not offered in the pilot: Java apps and "
        "chat bots (Discord, Telegram) do not build. Deploy a web app that answers HTTP on $PORT.",
        SECRET_IN_BUNDLE: "The build's secret scan found a secret in the source and stopped. "
        "Remove it from the code, rotate it, store it as an app secret and deploy again.",
        STATE_SQLITE_EPHEMERAL: SQLITE_ON_DISK,
        ADD_APPROVED_PACKAGE: "A dependency needs a system package (a native library, a font, "
        "a PDF tool) that is not on the platform package list, so the build stopped before it "
        "began. `ssc doctor` names the package and the dependency. " + HOW_TO_ASK,
    }
)

NOTICES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "SUPABASE_AUTH": "The app signs users in with Supabase. SSC signs everyone in already: "
        "read the user from the X-SSC-Identity header and remove the vendor sign-in.",
        "LOCALHOST_BIND": "The app seems to listen on localhost only. It must listen on 0.0.0.0 "
        "and the port in $PORT, or its health check fails at deploy.",
        "IN_PROCESS_SCHEDULER": "The app runs a scheduler inside its own process. Instances stop "
        "when idle, so its jobs would not run: declare them under [[schedules]] in ssc.toml.",
        "KV_STORE": "The app uses a key-value store, which SSC does not offer. Set postgres = true "
        "under [state] and keep the data in a table (for a cache, an UNLOGGED table with an "
        "expires_at column).",
        "DOCKERFILE_IGNORED": "The Dockerfile is not used: Railpack builds every image. For a "
        "system package the platform does not carry, ask with ADD_APPROVED_PACKAGE.",
    }
)
"""Warnings: the build goes on, and each is written to the build's log line."""
