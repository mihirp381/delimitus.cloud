"""``ssc deploy``: pack a folder, check it, upload it, build it for preview and deploy it.

Everything that can refuse the bundle runs before anything leaves the machine: the manifest,
packing (caps, links, ``.env`` files), the secret scan (``ssc_bundle.client.prepare``) and SQLite
on disk (``ssc_bundle.analyze.sqlite_on_disk``, the rule the build applies). Such a refusal exits
4 with the code the API would give, and ``status: null``. ``deploy`` always targets preview
(decision 017); production changes only through ``promote``.

The deployment's ``notice`` says what it sets off, such as the company's database being created
(SSC-087). The first deploy of an app also says how a sleeping app wakes up.

The build is always waited for, because only its release can be deployed. ``--wait`` also waits
for the deployment to be live. ``--build`` picks up a build an earlier run stopped waiting for:
it skips packing and upload, waits for that build and deploys its release.
"""

import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final

import typer

from ssc_bundle.analyze import MAX_ANALYZE_BYTES, sqlite_on_disk
from ssc_bundle.client import Prepared, SecretFoundError, prepare
from ssc_bundle.limits import DEFAULT_LIMITS, BundleError, BundleTooLargeError
from ssc_bundle.tarcheck import iter_entries
from ssc_cli.api import ApiClient
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.errors import (
    BAD_RESPONSE,
    BUILD_NOT_FOUND,
    CliError,
    ErrorBody,
    ExitCode,
    local_error,
)
from ssc_cli.models import BundleCreate, BundleOut
from ssc_cli.output import print_json, say
from ssc_cli.resolve import environment, resolve_app
from ssc_cli.shapes import BundleWarning, CapabilityChangeRow, DeployResult
from ssc_cli.wait import DEFAULT_TIMEOUT, Budget, wait_for_build, wait_for_operation
from ssc_contracts.build import SQLITE_ON_DISK, STATE_SQLITE_EPHEMERAL
from ssc_contracts.errors import ErrorCode
from ssc_contracts.manifest import ManifestError

PREVIEW: Final = "preview"
DEPLOY: Final = "deploy"
COMMIT: Final = re.compile(r"[0-9a-f]{40}")
BUILD_ID: Final = re.compile(r"bld_[a-z0-9]{20}")
MAX_DETAIL_LINES: Final = 5
WAKING: Final = (
    "The app sleeps when nobody uses it. The first visit after a quiet spell wakes it in a few "
    'seconds, and a browser shows a "waking up" page until it answers.'
)

type Note = Callable[[str], None]


def _commit(value: str | None) -> str | None:
    if value is not None and not COMMIT.fullmatch(value):
        raise typer.BadParameter("give the full 40-character lower-case hex commit id")
    return value


def _build_id(value: str | None) -> str | None:
    if value is not None and not BUILD_ID.fullmatch(value):
        raise typer.BadParameter("give a bld_ id")
    return value


PathArg = Annotated[
    Path,
    typer.Argument(
        help="The app folder.", exists=True, file_okay=False, dir_okay=True, resolve_path=True
    ),
]
AppOpt = Annotated[str, typer.Option("--app", help="App slug or app_ id.")]
CommitOpt = Annotated[
    str | None,
    typer.Option(
        "--commit", metavar="SHA", callback=_commit, help="The git commit the folder holds."
    ),
]
BuildOpt = Annotated[
    str | None,
    typer.Option(
        "--build",
        metavar="BUILD_ID",
        callback=_build_id,
        help="Deploy the release of this earlier preview build instead of the folder.",
    ),
]
WaitOpt = Annotated[bool, typer.Option("--wait", help="Also wait until the deployment is live.")]
TimeoutOpt = Annotated[
    int, typer.Option("--timeout", min=1, metavar="SECONDS", help="Give up waiting after this.")
]


def deploy(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to an option)
    ctx: typer.Context,
    app: AppOpt,
    path: PathArg = Path(),
    commit: CommitOpt = None,
    build: BuildOpt = None,
    wait: WaitOpt = False,
    timeout: TimeoutOpt = DEFAULT_TIMEOUT,
    json_mode: JsonOpt = False,
) -> None:
    """Upload the folder and deploy it to the app's preview environment."""
    if build is not None and commit is not None:
        raise typer.BadParameter("--commit goes with a folder, not with --build")
    s = session(ctx)
    note = _progress(json_mode)
    budget = Budget(timeout)
    with handled(json_mode), tempfile.TemporaryDirectory(prefix="ssc-deploy-") as tmp:
        prepared = None if build else prepare_folder(path, Path(tmp) / "bundle.tar.gz")
        if prepared is not None:
            b = prepared.bundle
            note(f"Packed {b.file_count} files, {b.size} bytes, {b.digest}.")
            for w in prepared.warnings:
                note(f"Warning: {w.rule} {w.masked} at {w.path}:{w.line} (not blocking).")
        with s.client() as client:
            target = resolve_app(client, app)
            env = environment(target, PREVIEW)
            if prepared is not None:
                started = _start(client, target.id, env.id, prepared, commit, note)
            else:
                started = _resume(client, target.id, env.id, str(build))
            resume = f"ssc deploy --app {target.slug} --build {started.build_id}"
            resume += " --wait" if wait else ""
            release_id, number = wait_for_build(
                client,
                started.build_id,
                sleep=s.sleep,
                budget=budget,
                next_step=f"Deploy its release once it is built with `{resume}`.",
            )
            digest = started.digest or client.get_release(target.id, release_id).source_digest
            note(f"Built R{number}. Deploying it to preview.")
            op = client.create_deployment(target.id, env.id, release_id, DEPLOY)
            if op.notice is not None:
                note(op.notice)
            state = op.state
            if wait:
                state = wait_for_operation(
                    client,
                    op.operation_id,
                    sleep=s.sleep,
                    budget=budget,
                    next_step=f"Follow it with `ssc status {target.slug}`.",
                    note=note,
                    told=op.notice,
                ).state
    result = DeployResult(
        app_id=target.id,
        slug=target.slug,
        environment=PREVIEW,
        environment_id=env.id,
        bundle_id=started.bundle_id,
        digest=digest,
        uploaded=started.uploaded,
        build_id=started.build_id,
        release_id=release_id,
        release_number=number,
        operation_id=op.operation_id,
        state=state,
        url=env.url,
        notice=op.notice,
        warnings=[
            BundleWarning(path=w.path, line=w.line, rule=w.rule, masked=w.masked)
            for w in (prepared.warnings if prepared is not None else ())
        ],
        capability_changes=started.changes,
    )
    if json_mode:
        print_json(result)
        return
    if state == "healthy":
        say(f"R{number} is live in preview of {target.slug}.")
    else:
        say(f"Deploying R{number} to preview of {target.slug} ({op.operation_id}).")
        say(f"Follow it with `ssc status {target.slug}`.")
    if env.url:
        say(f"Preview: {env.url}")
    if env.current_deployment_id is None:
        say(WAKING)


@dataclass(frozen=True, slots=True)
class _Started:
    """A preview build under way; ``digest`` is empty when the build was not started here."""

    bundle_id: str
    build_id: str
    digest: str
    uploaded: bool
    changes: list[CapabilityChangeRow]


def _start(  # noqa: PLR0913, PLR0917
    client: ApiClient, app_id: str, env_id: str, prepared: Prepared, commit: str | None, note: Note
) -> _Started:
    """Upload the bundle and start its preview build."""
    b = prepared.bundle
    body = BundleCreate(digest=b.digest, size_bytes=b.size, source_commit=commit)
    bundle, uploaded = upload_bundle(client, app_id, body, prepared, note)
    note(f"Building {bundle.bundle_id} for preview.")
    accepted = client.create_build(app_id, env_id, bundle.bundle_id)
    changes = [
        CapabilityChangeRow(
            severity=c.severity,
            kind=c.kind,
            subject=c.subject,
            consequence=c.consequence,
            approver=c.approver,
        )
        for c in accepted.capability_diff.changes
    ]
    for c in changes:
        note(f"Change: {c.consequence}")
    return _Started(bundle.bundle_id, accepted.build_id, b.digest, uploaded, changes)


def _resume(client: ApiClient, app_id: str, env_id: str, build_id: str) -> _Started:
    """An earlier preview build of this app, to wait for and deploy."""
    found = client.get_build(build_id)
    if (found.app_id, found.environment_id) != (app_id, env_id):
        raise local_error(
            BUILD_NOT_FOUND,
            "That build is not a preview build of this app.",
            f"Build {build_id} belongs to another app or environment.",
        )
    return _Started(found.bundle_id, build_id, "", False, [])


def _progress(json_mode: bool) -> Note:
    def note(text: str) -> None:
        if not json_mode:
            typer.echo(text, err=True)

    return note


def prepare_folder(root: Path, dest: Path) -> Prepared:
    """The packed and scanned folder, or a :class:`CliError` (exit 4) saying what blocks it."""
    try:
        prepared = prepare(root, dest)
        with dest.open("rb") as f:
            files = iter_entries(f, DEFAULT_LIMITS, MAX_ANALYZE_BYTES)
            sqlite = sqlite_on_disk(files, prepared.manifest)
    except ManifestError as e:
        lines = [str(p) for p in e.problems[:MAX_DETAIL_LINES]]
        raise _blocked(
            ErrorCode.MANIFEST_INVALID,
            "ssc.toml is not valid.",
            "\n".join(lines),
            "Run `ssc doctor`.",
        ) from None
    except SecretFoundError as e:
        found = [f"{f.rule} {f.masked} at {f.path}:{f.line}" for f in e.findings]
        raise _blocked(
            ErrorCode.SECRET_IN_BUNDLE,
            "The folder holds a secret, so nothing was uploaded.",
            "\n".join(found[:MAX_DETAIL_LINES]),
            "Take the value out of the source, or list the file in .sscignore if the app does "
            "not need it.",
        ) from None
    except BundleTooLargeError as e:
        raise _blocked(
            ErrorCode.BUNDLE_TOO_LARGE,
            "The folder is too large to deploy.",
            str(e),
            "List build output, data and media files in .sscignore.",
        ) from None
    except BundleError as e:
        raise _blocked(ErrorCode.BUNDLE_MALFORMED, "The folder cannot be packed.", str(e)) from None
    except OSError as e:
        raise _blocked(
            ErrorCode.BUNDLE_MALFORMED, "The folder cannot be read.", f"{e.filename}: {e.strerror}"
        ) from None
    if sqlite is not None:
        raise CliError(
            ErrorBody(
                code=STATE_SQLITE_EPHEMERAL,
                title="The app keeps SQLite on disk, so nothing was uploaded.",
                detail=f"{sqlite.detail} at {sqlite.path}",
            ),
            ExitCode.BLOCKED,
            SQLITE_ON_DISK,
        )
    return prepared


def _blocked(code: ErrorCode, title: str, detail: str, fix: str | None = None) -> CliError:
    return CliError(ErrorBody(code=code.value, title=title, detail=detail), ExitCode.BLOCKED, fix)


def upload_bundle(
    client: ApiClient, app_id: str, body: BundleCreate, prepared: Prepared, note: Note
) -> tuple[BundleOut, bool]:
    """The stored bundle, and whether its bytes were sent now."""
    bundle = client.create_bundle(app_id, body)
    if bundle.state == "stored":
        return bundle, False
    if bundle.upload is None:
        raise local_error(
            BAD_RESPONSE,
            "The API answered in a shape this ssc does not understand.",
            f"Bundle {bundle.bundle_id} is pending without an upload address.",
        )
    note("Uploading.")
    client.upload(bundle.upload, prepared.bundle.path)
    return client.complete_bundle(app_id, bundle.bundle_id), True
