"""The agent's configuration, metadata tokens, and the pure parts of the Cloud Run driver."""

import httpx2
import pytest

from ssc_agent.__main__ import ENV, LOG_VIEW_ENV, ConfigError, cell_from_env, log_views_from_env
from ssc_agent.cloud_run import (  # pyright: ignore[reportPrivateUsage]
    _bare,
    _cpu,
    _memory_mib,
    _readiness,
    _traffic,
)
from ssc_agent.metadata import MetadataAccessTokens, MetadataError

FULL = {name: f"value-{field}" for field, name in ENV.items()}


def test_cell_from_env_needs_every_name() -> None:
    cell = cell_from_env(FULL)
    assert cell.project == "value-project"
    assert cell.invoker == "value-invoker"
    for name in ENV.values():
        with pytest.raises(ConfigError, match=name):
            cell_from_env({k: v for k, v in FULL.items() if k != name})


def test_log_views_from_env_takes_one_or_more_views() -> None:
    app = "projects/cell-project/locations/us-central1/buckets/_Default/views/ssc-app-logs"
    builds = app.replace("ssc-app-logs", "ssc-build-logs")
    assert log_views_from_env({}) is None
    assert log_views_from_env({LOG_VIEW_ENV: app}) == (app,)
    assert log_views_from_env({LOG_VIEW_ENV: f"{app}, {builds}"}) == (app, builds)
    for bad in ("projects/cell-project/logs/run", f"{app},", f"{app},logs/x", "a b"):
        with pytest.raises(ConfigError, match=LOG_VIEW_ENV):
            log_views_from_env({LOG_VIEW_ENV: bad})


async def test_access_token_is_cached() -> None:
    calls: list[httpx2.Request] = []

    def metadata(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={"access_token": "tok", "expires_in": 3599})

    tokens = MetadataAccessTokens(httpx2.AsyncClient(transport=httpx2.MockTransport(metadata)))
    assert await tokens() == "tok"
    assert await tokens() == "tok"
    assert len(calls) == 1
    assert calls[0].headers["Metadata-Flavor"] == "Google"


async def test_access_token_failure_names_no_token() -> None:
    tokens = MetadataAccessTokens(
        httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _: httpx2.Response(403)))
    )
    with pytest.raises(MetadataError, match="HTTPStatusError"):
        await tokens()


def test_proto3_zeros_compare_equal() -> None:
    assert _bare({"scaling": {"minInstanceCount": 0, "maxInstanceCount": 2}}) == _bare(
        {"scaling": {"maxInstanceCount": 2}}
    )
    assert _bare({"invokerIamDisabled": False}) == _bare({})


@pytest.mark.parametrize(("value", "cpu"), [("1", 1.0), ("2", 2.0), ("1000m", 1.0), ("0.5", 0.5)])
def test_cpu(value: str, cpu: float) -> None:
    assert _cpu(value) == cpu


@pytest.mark.parametrize(("value", "mib"), [("512Mi", 512), ("2Gi", 2048), ("4096Mi", 4096)])
def test_memory(value: str, mib: int) -> None:
    assert _memory_mib(value) == mib


def test_latest_traffic_resolves_to_a_revision() -> None:
    svc = {
        "latestReadyRevision": "projects/p/locations/r/services/s/revisions/s-00002-abc",
        "trafficStatuses": [
            {"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 90},
            {"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION", "revision": "s-1", "percent": 10},
        ],
    }
    assert _traffic(svc) == {"s-00002-abc": 90, "s-1": 10}


@pytest.mark.parametrize(
    ("state", "seen"),
    [
        ("CONDITION_SUCCEEDED", (True, False)),
        ("CONDITION_FAILED", (False, True)),
        ("CONDITION_RECONCILING", (None, False)),
    ],
)
def test_readiness(state: str, seen: tuple[bool | None, bool]) -> None:
    assert _readiness({"conditions": [{"type": "Ready", "state": state}]}) == seen
