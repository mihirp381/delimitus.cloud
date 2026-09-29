"""The builder journey on the dev stack: token, create, share, status."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from ssc_cli.session import Session
from ssc_cli.shapes import AppResult, AppsResult, ShareResult, TokenSetResult, WhoamiResult

DEV_STACK = Path(__file__).resolve().parents[3] / "tools" / "dev_stack.py"


def test_journey_live(cli, live, isolated):
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
            "--api", live.url, *args, "--json", input=stdin, session=Session(sleep=lambda _: None)
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
