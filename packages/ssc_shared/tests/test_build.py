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
}


def test_a_build_round_trips_and_hides_its_url() -> None:
    build = build_from_wire(BUILD)
    assert build_to_wire(build) == BUILD
    assert "sig=" not in repr(build)
    with pytest.raises(TypeError):
        build.public_env["X"] = "y"  # type: ignore[index]
    assert build_from_wire({**BUILD, "start": None}).start is None


@pytest.mark.parametrize(
    "change",
    [
        {"build_id": "bld_short"},
        {"bundle_url": "http://control.test/b.tar.gz"},
        {"bundle_sha256": "A" * 64},
        {"public_env": {"lower": "x"}},
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
