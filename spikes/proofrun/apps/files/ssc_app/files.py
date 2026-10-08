"""Keep files through the cell's file broker (SSC-046). Ask for it with ``[files]`` in
``ssc.toml``.

    from ssc_app import files

    files.put("photos/cat.png", data, content_type="image/png")
    data = files.get("photos/cat.png")
    url = files.link("get", "photos/cat.png")["url"]   # send a browser there to download
    files.delete("photos/cat.png")

Each call asks the data gateway for a signed link with the app's own workload token
(``ssc_app.workload``), then sends the bytes to Cloud Storage with it; the app holds no storage
credentials. A file is at most 25 MB, a name is ``/``-separated segments of letters, digits,
``.``, ``_`` and ``-``, and a download always arrives as an attachment, never rendered as a page
(``docs/contracts/data-gateway.md#files``).

The data gateway's address comes from the metadata server: Cloud Run's ``instance/region``
names the project number and the region, and the gateway is ``ssc-datagw`` there.
``SSC_DATAGW_URL`` or ``url=`` replaces it. The data gateway runs from zero, so a call to it is
tried once more when it cannot be reached, times out, or answers 502, 503 or 504. Same names and
behaviour as the Node helper ``@delimitus/ssc-files``.
"""

import json
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Final, Literal

from ssc_app.workload import METADATA, TIMEOUT_SECONDS, WorkloadToken

SERVICE: Final = "ssc-datagw"
REGION_PATH: Final = "/computeMetadata/v1/instance/region"
URL_VARIABLE: Final = "SSC_DATAGW_URL"
GATEWAY_TIMEOUT_SECONDS: Final = 30.0
"""Long enough for the data gateway to start from zero."""
TRANSFER_TIMEOUT_SECONDS: Final = 120.0
RETRY_STATUSES: Final = frozenset({502, 503, 504})
DEFAULT_CONTENT_TYPE: Final = "application/octet-stream"

Op = Literal["put", "get", "delete"]


class FilesError(RuntimeError):
    """A refusal or a failure. ``code`` is the data gateway's (``FILE_NOT_FOUND``,
    ``FILES_QUOTA_EXCEEDED``, ``APP_NOT_ACTIVE``, ...), ``STORAGE_<status>`` when Cloud Storage
    refused the transfer, or ``UNREACHABLE``."""

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status = status


def gateway_url(metadata: str = METADATA) -> str:
    """``https://ssc-datagw-<project number>.<region>.run.app`` of the cell this app runs in."""
    request = urllib.request.Request(  # noqa: S310
        f"{metadata}{REGION_PATH}", headers={"Metadata-Flavor": "Google"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            answer = response.read().decode().strip()
    except (urllib.error.URLError, OSError) as exc:
        raise FilesError("UNREACHABLE", f"metadata server: {type(exc).__name__}") from None
    parts = answer.split("/")
    if len(parts) != 4 or parts[0] != "projects" or parts[2] != "regions":  # noqa: PLR2004
        raise FilesError("UNREACHABLE", "the metadata server did not name a region")
    return f"https://{SERVICE}-{parts[1]}.{parts[3]}.run.app"


def _send(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _refusal(status: int, raw: bytes) -> FilesError:
    try:
        error = json.loads(raw)["error"]
        return FilesError(str(error["code"]), str(error["message"]), status)
    except ValueError, KeyError, TypeError:
        return FilesError("UNAVAILABLE", f"the data gateway answered HTTP {status}", status)


class Files:
    """The file broker of the cell this app runs in. ``url`` and ``metadata`` replace the data
    gateway's address and the metadata server's (tests). Safe to share between threads."""

    def __init__(self, *, url: str | None = None, metadata: str = METADATA) -> None:
        self._url = (url or os.environ.get(URL_VARIABLE) or "").rstrip("/") or None
        self._metadata = metadata
        self._tokens: WorkloadToken | None = None
        self._lock = threading.Lock()

    def put(self, name: str, data: bytes, *, content_type: str = DEFAULT_CONTENT_TYPE) -> None:
        """Store ``data`` as ``name``, replacing a file of that name."""
        link = self.link("put", name, content_type=content_type)
        self._transfer(link, data)

    def get(self, name: str) -> bytes:
        """The bytes of ``name``; ``FilesError`` ``FILE_NOT_FOUND`` when there is none."""
        return self._transfer(self.link("get", name), None)

    def delete(self, name: str) -> None:
        """Remove ``name``; ``FilesError`` ``FILE_NOT_FOUND`` when there is none."""
        self._ask("delete", {"name": name})

    def link(self, op: Literal["put", "get"], name: str, **body: str) -> dict[str, Any]:
        """The signed link itself: ``url``, ``method``, ``headers`` to send, ``expires_at`` (10
        minutes), and ``max_bytes`` for ``put``. A ``get`` link suits a browser redirect."""
        return self._ask(op, {"name": name, **body})

    def _ask(self, op: Op, body: Mapping[str, str]) -> dict[str, Any]:
        url, tokens = self._target()
        raw_body = json.dumps(body).encode()
        for attempt in (1, 2):
            headers = {
                "authorization": f"Bearer {tokens.get()}",
                "content-type": "application/json",
            }
            try:
                status, raw = _send(
                    "POST", f"{url}/v1/files/{op}", headers, raw_body, GATEWAY_TIMEOUT_SECONDS
                )
            except (urllib.error.URLError, OSError) as exc:
                if attempt == 1:
                    continue
                raise FilesError("UNREACHABLE", f"data gateway: {type(exc).__name__}") from None
            if status in RETRY_STATUSES and attempt == 1:
                continue
            if status != 200:  # noqa: PLR2004
                raise _refusal(status, raw)
            return json.loads(raw)
        raise AssertionError

    def _transfer(self, link: Mapping[str, Any], data: bytes | None) -> bytes:
        try:
            status, raw = _send(
                link["method"], link["url"], link["headers"], data, TRANSFER_TIMEOUT_SECONDS
            )
        except (urllib.error.URLError, OSError) as exc:
            raise FilesError("UNREACHABLE", f"Cloud Storage: {type(exc).__name__}") from None
        if status == 404 and link["method"] == "GET":  # noqa: PLR2004
            raise FilesError("FILE_NOT_FOUND", "the file is gone", status)
        if not 200 <= status < 300:  # noqa: PLR2004
            raise FilesError(f"STORAGE_{status}", "Cloud Storage refused the transfer", status)
        return raw

    def _target(self) -> tuple[str, WorkloadToken]:
        with self._lock:
            if self._url is None:
                self._url = gateway_url(self._metadata)
            if self._tokens is None:
                self._tokens = WorkloadToken(audience=self._url, metadata=self._metadata)
            return self._url, self._tokens


_default: Files | None = None
_default_lock = threading.Lock()


def _files() -> Files:
    global _default  # noqa: PLW0603
    with _default_lock:
        if _default is None:
            _default = Files()
        return _default


def put(name: str, data: bytes, *, content_type: str = DEFAULT_CONTENT_TYPE) -> None:
    """``Files.put`` on this app's cell."""
    _files().put(name, data, content_type=content_type)


def get(name: str) -> bytes:
    """``Files.get`` on this app's cell."""
    return _files().get(name)


def delete(name: str) -> None:
    """``Files.delete`` on this app's cell."""
    _files().delete(name)


def link(op: Literal["put", "get"], name: str, **body: str) -> dict[str, Any]:
    """``Files.link`` on this app's cell."""
    return _files().link(op, name, **body)
