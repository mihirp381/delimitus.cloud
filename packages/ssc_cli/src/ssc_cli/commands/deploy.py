"""``ssc deploy``: pack a folder, check it, upload it, build it for preview and deploy it.

Everything that can refuse the bundle runs before anything leaves the machine: the manifest,
packing (caps, links, ``.env`` files) and the secret scan (``ssc_bundle.client.prepare``). Such a
refusal exits 4 with the code the API would give, and ``status: null``. ``deploy`` always
targets preview (decision 017); production changes only through ``promote``.

The build is always waited for, because only its release can be deployed. ``--wait`` also waits
for the deployment to be live.
"""

import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Final

import typer

from ssc_bundle.client import Prepared, SecretFoundError, prepare
from ssc_bundle.limits import BundleError, BundleTooLargeError
from ssc_cli.api import ApiClient
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.errors import BAD_RESPONSE, CliError, ErrorBody, ExitCode, local_error
from ssc_cli.models import BundleCreate, BundleOut
from ssc_cli.output import print_json, say
from ssc_cli.resolve import environment, resolve_app
from ssc_cli.shapes import BundleWarning, CapabilityChangeRow, DeployResult
from ssc_cli.wait import DEFAULT_TIMEOUT, wait_for_build, wait_for_operation
from ssc_contracts.errors import ErrorCode
from ssc_contracts.manifest import ManifestError

PREVIEW: Final = "preview"
DEPLOY: Final = "deploy"
COMMIT: Final = re.compile(r"[0-9a-f]{40}")
MAX_DETAIL_LINES: Final = 5

type Note = Callable[[str], None]


def _commit(value: str | None) -> str | None:
    if value is not None and not COMMIT.fullmatch(value):
        raise typer.BadParameter("give the full 40-character lower-case hex commit id")
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
WaitOpt = Annotated[bool, typer.Option("--wait", help="Also wait until the deployment is live.")]
TimeoutOpt = Annotated[
    int, typer.Option("--timeout", min=1, metavar="SECONDS", help="Give up waiting after this.")
]


def deploy(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to an option)
    ctx: typer.Context,
    app: AppOpt,
    path: PathArg = Path(),
    commit: CommitOpt = None,
    wait: WaitOpt = False,
    timeout: TimeoutOpt = DEFAULT_TIMEOUT,
    json_mode: JsonOpt = False,
) -> None:
    """Upload the folder and deploy it to the app's preview environment."""
    s = session(ctx)
    note = _progress(json_mode)
    with handled(json_mode), tempfile.TemporaryDirectory(prefix="ssc-deploy-") as tmp:
        prepared = _prepare(path, Path(tmp) / "bundle.tar.gz")
        b = prepared.bundle
        note(f"Packed {b.file_count} files, {b.size} bytes, {b.digest}.")
        for w in prepared.warnings:
            note(f"Warning: {w.rule} {w.masked} at {w.path}:{w.line} (not blocking).")
        with s.client() as client:
            target = resolve_app(client, app)
            env = environment(target, PREVIEW)
            body = BundleCreate(digest=b.digest, size_bytes=b.size, source_commit=commit)
            bundle, uploaded = _upload(client, target.id, body, prepared, note)
            note(f"Building {bundle.bundle_id} for preview.")
            accepted = client.create_build(target.id, env.id, bundle.bundle_id)
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
            follow = f"ssc status {target.slug}"
            release_id, number = wait_for_build(
                client, accepted.build_id, sleep=s.sleep, timeout=timeout, follow=follow
            )
            note(f"Built R{number}. Deploying it to preview.")
            op = client.create_deployment(target.id, env.id, release_id, DEPLOY)
            state = op.state
            if wait:
                state = wait_for_operation(
                    client, op.operation_id, sleep=s.sleep, timeout=timeout, follow=follow
                ).state
    result = DeployResult(
        app_id=target.id,
        slug=target.slug,
        environment=PREVIEW,
        environment_id=env.id,
        bundle_id=bundle.bundle_id,
        digest=b.digest,
        uploaded=uploaded,
        build_id=accepted.build_id,
        release_id=release_id,
        release_number=number,
        operation_id=op.operation_id,
        state=state,
        url=env.url,
        warnings=[
            BundleWarning(path=w.path, line=w.line, rule=w.rule, masked=w.masked)
            for w in prepared.warnings
        ],
        capability_changes=changes,
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


def _progress(json_mode: bool) -> Note:
    def note(text: str) -> None:
        if not json_mode:
            typer.echo(text, err=True)

    return note


def _prepare(root: Path, dest: Path) -> Prepared:
    try:
        return prepare(root, dest)
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


def _blocked(code: ErrorCode, title: str, detail: str, fix: str | None = None) -> CliError:
    return CliError(ErrorBody(code=code.value, title=title, detail=detail), ExitCode.BLOCKED, fix)


def _upload(
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
