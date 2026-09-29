"""The dev stack's API: dev settings, a metrics key and a filesystem blob store by default."""

import base64
import json
import stat
import uuid

import httpx2
import psycopg

from ssc_bundle.client import prepare
from ssc_cli.credentials import SERVICE
from ssc_cli.session import Session
from ssc_control.api import Settings
from ssc_control.api.routes.blobs import blob_store_for, check_fs_allowed
from ssc_shared.blobstore_fs import FsBlobStore

BASE = {"SSC_DATABASE_DSN": "postgresql://ssc_app@localhost/ssc", "SSC_API_JWKS": '{"keys": []}'}


def _key_bytes(value: str) -> int:
    return len(base64.b64decode(value, validate=True))


def test_serve_defaults_record_metrics_and_store_blobs(dev_stack, tmp_path):
    stack = dev_stack
    env = stack.api_env(tmp_path, dict(BASE))
    assert env["SSC_ENV"] == "dev"
    assert _key_bytes(env["SSC_METRICS_KEY"]) == 32
    assert env["SSC_BLOB_BACKEND"] == "fs"
    assert env["SSC_BLOB_ROOT"] == str(tmp_path / "blobs")
    keys = json.loads(env["SSC_BLOB_SIGNING_KEYS"])
    assert list(keys) == [env["SSC_BLOB_SIGNING_KID"]]
    assert _key_bytes(keys[env["SSC_BLOB_SIGNING_KID"]]) == 32
    assert env["SSC_METRICS_KEY"] != keys[env["SSC_BLOB_SIGNING_KID"]]

    settings = Settings.from_env(env)
    assert settings.metrics_key is not None
    store = blob_store_for(settings)
    assert isinstance(store, FsBlobStore)
    check_fs_allowed(store, settings)

    # Kept in the private state file and reused, so pseudonyms and URLs survive a restart.
    assert stack.api_env(tmp_path, dict(BASE)) == env
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600


def test_what_the_environment_sets_wins(dev_stack, tmp_path):
    stack = dev_stack
    given = {**BASE, "SSC_BLOB_BACKEND": "none", "SSC_METRICS_KEY": "k", "SSC_ENV": "test"}
    env = stack.api_env(tmp_path, given)
    assert {k: env[k] for k in ("SSC_BLOB_BACKEND", "SSC_METRICS_KEY", "SSC_ENV")} == {
        "SSC_BLOB_BACKEND": "none",
        "SSC_METRICS_KEY": "k",
        "SSC_ENV": "test",
    }


def test_live_bundle_upload(live, tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    (folder / "ssc.toml").write_text('schema = "ssc/v1"\n')
    (folder / "main.py").write_text("print('hello')\n")
    prepared = prepare(folder, tmp_path / "bundle.tar.gz")
    bundle = prepared.bundle
    headers = {"authorization": f"Bearer {live.token()}"}
    with httpx2.Client(base_url=live.url, headers=headers) as http:
        app = http.post(
            "/v1/apps",
            json={"slug": f"b{uuid.uuid4().hex[:12]}"},
            headers={"idempotency-key": str(uuid.uuid4())},
        )
        assert app.status_code == 201, app.text
        bundles = f"/v1/apps/{app.json()['id']}/bundles"
        asked = http.post(
            bundles,
            json={"digest": bundle.digest, "size_bytes": bundle.size},
            headers={"idempotency-key": str(uuid.uuid4())},
        )
        assert asked.status_code == 201, asked.text
        upload = asked.json()["upload"]
        assert upload["url"].startswith(f"{live.url}/blobs/")
        put = httpx2.put(upload["url"], content=bundle.path.read_bytes(), headers=upload["headers"])
        assert put.status_code == 201, put.text
        done = http.post(
            f"{bundles}/{asked.json()['bundle_id']}/complete",
            headers={"idempotency-key": str(uuid.uuid4())},
        )
        assert done.status_code == 200, done.text
        assert (done.json()["state"], done.json()["manifest_digest"]) == (
            "stored",
            prepared.manifest_digest,
        )


def test_live_share_records_the_cli_as_source_tool(cli, live, isolated):
    isolated.set_password(SERVICE, live.url, live.token())
    session = Session(api_override=live.url, sleep=lambda _: None)
    name = f"m{uuid.uuid4().hex[:12]}"
    created = cli("apps", "create", name, "--json", session=session)
    assert created.code == 0, created.stdout
    assert cli("share", name, "--org", "--json", session=session).code == 0
    dsn = live.stack.load_state(live.dir)["database_dsn"]
    with psycopg.connect(dsn) as conn:
        conn.execute("select set_config('ssc.org', %s, true)", (live.org_id,))
        tools = conn.execute(
            "select source_tool from ssc.metrics_event where kind = 'share' and app_id = %s",
            (created.json()["id"],),
        ).fetchall()
    assert tools == [("ssc-cli",)]
