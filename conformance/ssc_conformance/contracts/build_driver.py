"""``BuildDriver`` contract. The fake passes it now; the Cloud Build driver (SSC-015) must too.

Subclass ``BuildDriverContract`` as a ``Test*`` class and provide three fixtures:

- ``build_driver``: the driver under test;
- ``buildable``: a callable giving a fresh ``BuildRequest`` for a bundle that builds;
- ``broken``: a callable giving a fresh ``BuildRequest`` for a bundle whose build fails (a
  cloud builder uses one of the SSC-015 failing fixtures).

Polling waits ``pause`` seconds between polls (the fake needs none), up to ``POLL_LIMIT`` polls.
"""

import asyncio
import re
from collections.abc import Callable
from typing import Final

import pytest

from ssc_contracts.ids import new_id
from ssc_control.deploy.build_driver import (
    MAX_REF_CHARS,
    BuildDriver,
    BuildNotFoundError,
    BuildRequest,
    BuildStatus,
    Failed,
    Running,
    Succeeded,
)

type NewRequest = Callable[[], BuildRequest]

POLL_LIMIT: Final = 360
_CODE: Final = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")


async def until_done(driver: BuildDriver, ref: str, pause: float) -> Succeeded | Failed:
    """Poll ``ref`` until the build ends."""
    for _ in range(POLL_LIMIT):
        status: BuildStatus = await driver.poll(ref)
        if not isinstance(status, Running):
            return status
        await asyncio.sleep(pause)
    pytest.fail(f"{ref} still running after {POLL_LIMIT} polls")


class BuildDriverContract:
    """Behaviour every ``BuildDriver`` shows. Test methods take the fixtures by name."""

    pause: float = 0.0

    async def test_a_build_ends_with_a_pinned_image(
        self, build_driver: BuildDriver, buildable: NewRequest
    ) -> None:
        ref = await build_driver.start(buildable())
        assert 1 <= len(ref) <= MAX_REF_CHARS
        result = await until_done(build_driver, ref, self.pause)
        assert isinstance(result, Succeeded), result
        assert _DIGEST.fullmatch(result.image_digest)
        assert all(result.scan_refs)

    async def test_starting_the_same_build_twice_is_one_build(
        self, build_driver: BuildDriver, buildable: NewRequest
    ) -> None:
        request = buildable()
        first = await build_driver.start(request)
        assert await build_driver.start(request) == first
        other = await build_driver.start(buildable())
        assert other != first

    async def test_a_broken_build_fails_with_a_reason_code(
        self, build_driver: BuildDriver, broken: NewRequest
    ) -> None:
        ref = await build_driver.start(broken())
        result = await until_done(build_driver, ref, self.pause)
        assert isinstance(result, Failed), result
        assert _CODE.fullmatch(result.code)

    async def test_a_finished_build_stays_finished(
        self, build_driver: BuildDriver, buildable: NewRequest, broken: NewRequest
    ) -> None:
        for request in (buildable(), broken()):
            ref = await build_driver.start(request)
            result = await until_done(build_driver, ref, self.pause)
            assert await build_driver.poll(ref) == result

    async def test_an_unknown_ref_is_not_found(self, build_driver: BuildDriver) -> None:
        with pytest.raises(BuildNotFoundError):
            await build_driver.poll(f"no-such-build-{new_id('bld')}")
