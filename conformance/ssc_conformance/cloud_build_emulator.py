"""An in-memory Cloud Build API v1 (regional builds), for ``httpx2.MockTransport``.

It models what ``CloudBuildDriver`` relies on: a create answers with an Operation whose
metadata holds the queued build; a build runs on its first get and ends after ``polls`` more;
``tags="..."`` filters a list. Running a build fetches the bundle from the ``SSC_BUNDLE_URL`` of
its ``fetch`` step through ``fetch`` (the test's stand-in for the signed-URL route) and checks
its sha256, as the step does; a failed fetch fails the ``fetch`` step with curl's exit 22.
``fail(sha256, exit_code)`` makes builds of that bundle fail in the step that would exit so.
"""

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Callable
from typing import Any, Final

import httpx2

type Json = dict[str, Any]

PROJECT: Final = "ssc-c-emulated"
REGION: Final = "us-central1"
CURL_HTTP_ERROR: Final = 22
_OPERATION_METADATA: Final = (
    "type.googleapis.com/google.devtools.cloudbuild.v1.BuildOperationMetadata"
)
_STEP_OF_EXIT: Final = {10: "scan", 11: "build", 12: "build", 13: "plan", 14: "build"}
_BUILDS = re.compile(rf"/v1/projects/{PROJECT}/locations/{REGION}/builds(?:/([^/]+))?")
_TAG_FILTER = re.compile(r'tags="([^"]+)"')


class CloudBuildEmulator:
    def __init__(self, fetch: Callable[[str], bytes], *, polls: int = 1) -> None:
        self.builds: dict[str, Json] = {}
        self.calls: list[tuple[str, str]] = []
        self.polls = polls
        self._fetch = fetch
        self._failing: dict[str, int] = {}
        self._seen: dict[str, int] = {}

    def fail(self, sha256: str, exit_code: int) -> None:
        self._failing[sha256] = exit_code

    def handler(self, request: httpx2.Request) -> httpx2.Response:  # noqa: PLR0911  (one per route)
        self.calls.append((request.method, request.url.path))
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return _error(401, "UNAUTHENTICATED")
        m = _BUILDS.fullmatch(request.url.path)
        if m is None:
            return _error(404, "NOT_FOUND")
        build_id = m.group(1)
        if build_id is None and request.method == "POST":
            return self._create(json.loads(request.content))
        if build_id is None and request.method == "GET":
            tag = _TAG_FILTER.fullmatch(request.url.params.get("filter", ""))
            found = [b for b in self.builds.values() if tag and tag.group(1) in b["tags"]]
            return httpx2.Response(200, json={"builds": copy.deepcopy(found)} if found else {})
        if build_id is not None and request.method == "GET":
            build = self.builds.get(build_id)
            if build is None:
                return _error(404, "NOT_FOUND")
            self._advance(build)
            return httpx2.Response(200, json=copy.deepcopy(build))
        return _error(405, "METHOD_NOT_ALLOWED")

    def _create(self, body: Json) -> httpx2.Response:
        if "source" in body or not body.get("steps") or not body.get("serviceAccount"):
            return _error(400, "INVALID_ARGUMENT")
        build_id = str(uuid.uuid4())
        build = {
            **copy.deepcopy(body),
            "id": build_id,
            "name": f"projects/{PROJECT}/locations/{REGION}/builds/{build_id}",
            "projectId": PROJECT,
            "status": "QUEUED",
        }
        self.builds[build_id] = build
        self._seen[build_id] = 0
        operation = {
            "name": f"projects/{PROJECT}/locations/{REGION}/operations/{uuid.uuid4()}",
            "metadata": {"@type": _OPERATION_METADATA, "build": copy.deepcopy(build)},
        }
        return httpx2.Response(200, json=operation)

    def _advance(self, build: Json) -> None:
        if build["status"] not in ("QUEUED", "WORKING"):
            return
        seen = self._seen[build["id"]]
        self._seen[build["id"]] = seen + 1
        if seen == 0:
            build["status"] = "WORKING"
            build["_exit"] = self._run(build)
        if seen < self.polls:
            return
        exit_code, step = build.pop("_exit")
        if exit_code == 0:
            build["status"] = "SUCCESS"
            build["results"] = {
                "images": [{"name": image, "digest": _digest(image)} for image in build["images"]]
            }
            for s in build["steps"]:
                s["status"] = "SUCCESS"
            return
        build["status"] = "FAILURE"
        index = [s["id"] for s in build["steps"]].index(step)
        build["failureInfo"] = {
            "type": "USER_BUILD_STEP",
            "detail": f'Build step failure: build step {index} "{step}" failed: step exited with '
            f"non-zero status: {exit_code}",
        }
        for i, s in enumerate(build["steps"]):
            s["status"] = "SUCCESS" if i < index else "FAILURE" if i == index else "QUEUED"

    def _run(self, build: Json) -> tuple[int, str]:
        env = dict(e.replace("$$", "$").split("=", 1) for e in build["steps"][0]["env"])
        try:
            data = self._fetch(env["SSC_BUNDLE_URL"])
        except Exception:
            return CURL_HTTP_ERROR, "fetch"
        sha256 = hashlib.sha256(data).hexdigest()
        if sha256 != env["SSC_BUNDLE_SHA256"]:
            return 1, "fetch"
        exit_code = self._failing.get(sha256, 0)
        return exit_code, _STEP_OF_EXIT.get(exit_code, "build")


def _digest(image: str) -> str:
    return "sha256:" + hashlib.sha256(image.encode()).hexdigest()


def _error(status: int, code: str) -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"code": status, "status": code}})
