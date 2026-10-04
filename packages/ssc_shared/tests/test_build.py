"""SSC-015: what the control plane sends a cell to build, and the status it reads back."""

from typing import Any

import pytest

from ssc_shared.build import (
    BuildStatus,
    CellBuild,
    Failed,
    Running,
    Succeeded,
    build_from_wire,
    build_to_wire,
    status_from_wire,
    status_to_wire,
)

BUILD: dict[str, Any] = {
    "build_id": "bld_" + "a" * 20,
    "bundle_url": "https://control.test/v1/blobs/b.tar.gz?exp=1&sig=x",
    "bundle_sha256": "0" * 64,
    "public_env": {"VITE_API": "https://api.example.com"},
    "start": "python app.py",
    "system_packages": ["pkg-config", "poppler-utils"],
}


def test_a_build_round_trips_and_hides_its_url() -> None:
    build = build_from_wire(BUILD)
    assert build_to_wire(build) == BUILD
    assert "sig=" not in repr(build)
    with pytest.raises(TypeError):
        build.public_env["X"] = "y"  # type: ignore[index]
    assert build_from_wire({**BUILD, "start": None}).start is None


def test_a_build_from_an_older_control_plane_installs_no_package() -> None:
    older = {k: v for k, v in BUILD.items() if k != "system_packages"}
    assert build_from_wire(older).system_packages == ()


def test_only_packages_on_the_platform_list_are_installed() -> None:
    fields = {**BUILD, "system_packages": ("poppler-utils", "tesseract-ocr")}
    with pytest.raises(ValueError, match="tesseract-ocr"):
        CellBuild(**fields)
    with pytest.raises(ValueError, match="tesseract-ocr"):
        build_from_wire({**BUILD, "system_packages": ["tesseract-ocr"]})


@pytest.mark.parametrize(
    "change",
    [
        {"build_id": "bld_short"},
        {"bundle_url": "http://control.test/b.tar.gz"},
        {"bundle_sha256": "A" * 64},
        {"public_env": {"lower": "x"}},
        {"system_packages": ["curl"]},
    ],
)
def test_a_malformed_build_is_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        CellBuild(**{**BUILD, **change})
    with pytest.raises(ValueError):
        build_from_wire({**BUILD, **change})


@pytest.mark.parametrize(
    "body",
    [
        {k: v for k, v in BUILD.items() if k != "start"},
        {**BUILD, "build_id": 7},
        {**BUILD, "public_env": ["VITE_API"]},
        {**BUILD, "public_env": {"VITE_API": 1}},
        {**BUILD, "start": 1},
        {**BUILD, "system_packages": "poppler-utils"},
        {**BUILD, "system_packages": [1]},
    ],
)
def test_a_build_with_missing_or_mistyped_fields_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        build_from_wire(body)


@pytest.mark.parametrize(
    "status",
    [
        Running(),
        Succeeded(image_digest="sha256:" + "b" * 64, scan_refs=("scan-1",)),
        Failed(code="BUILD_NO_ENTRYPOINT", message="no start command"),
    ],
)
def test_a_status_round_trips(status: BuildStatus) -> None:
    assert status_from_wire(status_to_wire(status)) == status


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"state": "queued"},
        {"state": "succeeded", "image_digest": "sha256:" + "b" * 64},
        {"state": "succeeded", "image_digest": "sha256:" + "b" * 64, "scan_refs": "x"},
        {"state": "succeeded", "image_digest": "latest", "scan_refs": []},
        {"state": "failed", "code": "lower", "message": "x"},
        {"state": "failed", "code": "BUILD_TIMED_OUT"},
    ],
)
def test_a_malformed_status_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        status_from_wire(body)
