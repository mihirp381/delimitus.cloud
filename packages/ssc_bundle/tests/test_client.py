"""SSC-014: ``ssc deploy`` refuses locally, before any upload."""

from pathlib import Path

import pytest

from ssc_bundle.client import Prepared, SecretFoundError, prepare, ship
from ssc_bundle.limits import BundleTooLargeError, Limits
from ssc_contracts.manifest import ManifestError

AWS_KEY = "AKIA" + "Z7Q3K9XW" + "P2LMN4RT"
PUBLIC = "q8ZrT2vN" + "x7LpW4mK" + "s9HdB3jF"
PASSWORD = "Sup3r" + "Secr3tPw"


class Spy:
    def __init__(self) -> None:
        self.calls: list[Prepared] = []

    def __call__(self, prepared: Prepared) -> str:
        self.calls.append(prepared)
        return "uploaded"


def app(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "app"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def test_a_secret_stops_the_deploy_before_upload(tmp_path: Path) -> None:
    root = app(tmp_path, {"src/app.js": f'const key = "{AWS_KEY}";\n'})
    dest, spy = tmp_path / "b.tar.gz", Spy()
    with pytest.raises(SecretFoundError) as e:
        ship(root, dest, spy)
    assert spy.calls == [] and not dest.exists()
    assert e.value.reason == "secret" and e.value.path == "src/app.js"
    assert AWS_KEY not in str(e.value) and "AKIA…RT" in str(e.value)
    assert [f.rule for f in e.value.findings] == ["aws_access_key"]


def test_values_declared_public_are_not_secrets(tmp_path: Path) -> None:
    code = {"src/app.js": f'const api_key = "{PUBLIC}";\n'}
    with pytest.raises(SecretFoundError):
        prepare(app(tmp_path / "a", code), tmp_path / "a.tar.gz")
    manifest = f'schema = "ssc/v1"\n\n[build.public_env.prod]\nVITE_MAPS_KEY = "{PUBLIC}"\n'
    root = app(tmp_path / "b", {**code, "ssc.toml": manifest})
    prepared = prepare(root, tmp_path / "b.tar.gz")
    assert prepared.warnings == ()
    assert prepared.manifest.build.public_env["prod"]["VITE_MAPS_KEY"] == PUBLIC
    assert prepared.manifest_digest.startswith("sha256:")


def test_a_local_database_url_warns_and_still_uploads(tmp_path: Path) -> None:
    url = "postgres://app:" + PASSWORD + "@localhost:5432/app"
    root = app(tmp_path, {"README.md": f"Run against {url}\n"})
    dest, spy = tmp_path / "b.tar.gz", Spy()
    assert ship(root, dest, spy) == "uploaded"
    (prepared,) = spy.calls
    assert [(f.rule, f.blocking) for f in prepared.warnings] == [("db_url_with_password", False)]
    assert prepared.bundle.path == dest and dest.exists()


def test_an_oversized_app_is_refused_before_upload(tmp_path: Path) -> None:
    root = app(tmp_path, {"a.txt": "a", "b.txt": "b"})
    dest, spy = tmp_path / "b.tar.gz", Spy()
    with pytest.raises(BundleTooLargeError):
        ship(root, dest, spy, limits=Limits(max_files=1))
    assert spy.calls == [] and not dest.exists()


def test_an_invalid_manifest_is_refused_before_packing(tmp_path: Path) -> None:
    root = app(tmp_path, {"ssc.toml": 'schema = "ssc/v9"\n', "app.py": "print(1)\n"})
    dest, spy = tmp_path / "b.tar.gz", Spy()
    with pytest.raises(ManifestError):
        ship(root, dest, spy)
    assert spy.calls == [] and not dest.exists()
