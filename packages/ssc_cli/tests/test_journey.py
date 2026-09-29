"""The builder journey on the dev stack: token, create, share, deploy, releases, rollback, promote.

The dev stack runs the worker with the fake builder and runtime, so a deploy goes all the way to
a healthy deployment. Polls sleep briefly for real so the worker gets to run.
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ssc_cli.session import Session
from ssc_cli.shapes import (
    AppResult,
    AppsResult,
    DeployResult,
    PromoteResult,
    ReleasesResult,
    RollbackResult,
    ShareResult,
    TokenSetResult,
    WhoamiResult,
)

DEV_STACK = Path(__file__).resolve().parents[3] / "tools" / "dev_stack.py"


def _short_sleep(_: float) -> None:
    time.sleep(0.1)


def test_journey_live(cli, live, isolated, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SSC_")}
    token = subprocess.run(
        [sys.executable, str(DEV_STACK), "--dir", str(live.dir), "token"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout

    def ssc(*args: str, stdin: str | None = None) -> dict[str, object]:
        r = cli(
            "--api", live.url, *args, "--json", input=stdin, session=Session(sleep=_short_sleep)
        )
        assert r.code == 0, (args, r.stdout, r.stderr)
        return json.loads(r.stdout)

    saved = TokenSetResult.model_validate(ssc("token", "set", stdin=token))
    assert (saved.org_id, saved.subject) == (live.org_id, live.admin_id)
    assert isolated.get_password("ssc", live.url) == token.strip()

    me = WhoamiResult.model_validate(ssc("whoami"))
    assert (me.org_id, me.subject, me.kind) == (live.org_id, live.admin_id, "user")

    slug = f"j{uuid.uuid4().hex[:12]}"
    created = AppResult.model_validate(ssc("apps", "create", slug))
    assert created.owner_user_id == live.admin_id
    assert sorted(e.name for e in created.environments) == ["preview", "prod"]
    assert slug in {a.slug for a in AppsResult.model_validate(ssc("apps")).apps}

    to_org = ShareResult.model_validate(ssc("share", slug, "--org"))
    assert to_org.changed
    to_me = ShareResult.model_validate(ssc("share", created.id, live.admin_id, "--env", "preview"))
    assert [(g.role, g.subject_kind, g.subject_id) for g in to_me.grants] == [
        ("builder", "user", live.admin_id)
    ]
    assert not ShareResult.model_validate(ssc("share", slug, "--org")).changed

    status = AppResult.model_validate(ssc("status", slug))
    versions = {e.name: e.grants_version for e in status.environments}
    before = {e.name: e.grants_version for e in created.environments}
    assert versions == {"prod": before["prod"] + 1, "preview": before["preview"] + 1}
    assert all(e.deployment is None for e in status.environments)

    gone = ShareResult.model_validate(ssc("unshare", slug, "--org"))
    assert gone.changed
    assert gone.grants == []

    folder = tmp_path / "app"
    folder.mkdir()
    (folder / "ssc.toml").write_text('schema = "ssc/v1"\n')
    (folder / "main.py").write_text("print('one')\n")
    first = DeployResult.model_validate(ssc("deploy", str(folder), "--app", slug, "--wait"))
    assert (first.state, first.release_number, first.uploaded) == ("healthy", 1, True)
    assert first.url is not None
    assert re.fullmatch(rf"https://{slug}--preview\.[a-z]{{12}}\.[a-z.]+", first.url)
    again = DeployResult.model_validate(ssc("deploy", str(folder), "--app", slug, "--wait"))
    assert (again.uploaded, again.digest, again.release_number) == (False, first.digest, 2)

    (folder / "main.py").write_text("print('two')\n")
    commit = "c" * 40
    third = DeployResult.model_validate(
        ssc("deploy", str(folder), "--app", slug, "--wait", "--commit", commit)
    )
    assert (third.state, third.release_number, third.uploaded) == ("healthy", 3, True)

    status = AppResult.model_validate(ssc("status", slug))
    envs = {e.name: e for e in status.environments}
    assert envs["preview"].deployment is not None
    assert (envs["preview"].deployment.state, envs["preview"].deployment.release_id) == (
        "healthy",
        third.release_id,
    )
    assert envs["preview"].url == first.url
    assert envs["prod"].deployment is None

    listed = ReleasesResult.model_validate(ssc("releases", slug))
    assert [(r.number, r.built_for, r.live_in) for r in listed.releases] == [
        (3, "preview", ["preview"]),
        (2, "preview", []),
        (1, "preview", []),
    ]
    assert listed.releases[0].source_commit == commit
    assert listed.next_before is None
    paged = ReleasesResult.model_validate(ssc("releases", slug, "--limit", "1", "--before", "3"))
    assert ([r.number for r in paged.releases], paged.next_before) == ([2], 2)

    back = RollbackResult.model_validate(ssc("rollback", slug, "R1", "--wait"))
    assert (back.environment, back.release_number, back.state) == ("preview", 1, "healthy")
    after = ReleasesResult.model_validate(ssc("releases", slug))
    assert {r.number: r.live_in for r in after.releases} == {1: ["preview"], 2: [], 3: []}

    promoted = PromoteResult.model_validate(ssc("promote", slug, "--wait"))
    assert (promoted.state, promoted.release_number) == ("healthy", 4)
    assert promoted.source_release_id == first.release_id
    assert promoted.url is not None
    assert re.fullmatch(rf"https://{slug}\.[a-z]{{12}}\.[a-z.]+", promoted.url)
    rows = {r.number: r for r in ReleasesResult.model_validate(ssc("releases", slug)).releases}
    assert (rows[4].built_for, rows[4].live_in) == ("prod", ["prod"])
    assert rows[4].source_digest == rows[1].source_digest
    assert rows[4].image_digest != rows[1].image_digest
