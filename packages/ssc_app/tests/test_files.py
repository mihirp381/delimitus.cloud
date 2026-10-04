"""The Python file helper (SSC-046) against one local server that stands in for the metadata
server, the data gateway and Cloud Storage, so a link it hands out points back at itself."""

import base64
import http.server
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest

from ssc_app import files
from ssc_app.files import REGION_PATH, Files, FilesError, gateway_url
from ssc_app.workload import IDENTITY_PATH

NUMBER, REGION = "123456789012", "us-central1"


def _token(audience: str) -> str:
    def part(doc: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(doc).encode()).rstrip(b"=").decode()

    claims = {"aud": audience, "exp": 4_000_000_000, "email": "ssc-a-x@p.iam.gserviceaccount.com"}
    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2ln"


@dataclass
class Cell:
    """What the stand-in was asked, what it keeps, and what it answers next."""

    url: str = ""
    region: str = f"projects/{NUMBER}/regions/{REGION}"
    audiences: list[str] = field(default_factory=list[str])
    asked: list[tuple[str, dict[str, str], str]] = field(
        default_factory=list[tuple[str, dict[str, str], str]]
    )
    gateway_answers: list[tuple[int, bytes]] = field(default_factory=list[tuple[int, bytes]])
    storage_status: int | None = None
    objects: dict[str, tuple[bytes, str]] = field(default_factory=dict[str, tuple[bytes, str]])
    sent_headers: list[dict[str, str]] = field(default_factory=list[dict[str, str]])


def refusal(code: str, status: int = 404) -> tuple[int, bytes]:
    body = {"error": {"code": code, "message": "fixed", "stage": "files", "fix_owner": "app"}}
    return status, json.dumps(body).encode()


@pytest.fixture
def cell() -> Iterator[Cell]:
    state = Cell()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self, status: int, body: bytes = b"", **headers: str) -> None:
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k.replace("_", "-"), v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path.startswith("/storage/"):
                self._storage_get(url.path.removeprefix("/storage/"))
                return
            if self.headers.get("Metadata-Flavor") != "Google":
                self._answer(403)
            elif url.path == REGION_PATH:
                self._answer(200, state.region.encode())
            elif url.path == IDENTITY_PATH:
                audience = parse_qs(url.query)["audience"][0]
                state.audiences.append(audience)
                self._answer(200, _token(audience).encode())
            else:
                self._answer(404)

        def do_POST(self) -> None:
            op = self.path.removeprefix("/v1/files/")
            raw = self.rfile.read(int(self.headers["content-length"])).decode()
            state.asked.append((op, {k.lower(): v for k, v in self.headers.items()}, raw))
            if state.gateway_answers:
                self._answer(*state.gateway_answers.pop(0))
                return
            body = json.loads(raw)
            name = body["name"]
            if op == "delete":
                found = state.objects.pop(name, None) is not None
                self._answer(*((200, b'{"deleted": true}') if found else refusal("FILE_NOT_FOUND")))
                return
            if op == "get" and name not in state.objects:
                self._answer(*refusal("FILE_NOT_FOUND"))
                return
            link = {
                "url": f"{state.url}/storage/{name}",
                "method": "PUT" if op == "put" else "GET",
                "headers": {"content-type": body["content_type"], "x-check": "signed"}
                if op == "put"
                else {},
                "expires_at": "2026-10-03T12:10:00Z",
            }
            self._answer(200, json.dumps(link).encode())

        def do_PUT(self) -> None:
            state.sent_headers.append({k.lower(): v for k, v in self.headers.items()})
            data = self.rfile.read(int(self.headers["content-length"]))
            if state.storage_status is not None:
                self._answer(state.storage_status)
                return
            name = self.path.removeprefix("/storage/")
            state.objects[name] = (data, self.headers["content-type"])
            self._answer(200)

        def _storage_get(self, name: str) -> None:
            if state.storage_status is not None or name not in state.objects:
                self._answer(state.storage_status or 404)
                return
            data, kind = state.objects[name]
            self._answer(200, data, content_type=kind, content_disposition="attachment")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def helper(cell: Cell) -> Files:
    return Files(url=cell.url, metadata=cell.url)


def test_the_data_gateway_is_found_from_the_metadata_server(cell: Cell) -> None:
    assert gateway_url(cell.url) == f"https://ssc-datagw-{NUMBER}.{REGION}.run.app"


def test_a_metadata_server_that_names_no_region_is_an_error(cell: Cell) -> None:
    cell.region = "us-central1"
    with pytest.raises(FilesError, match="region"):
        gateway_url(cell.url)
    with pytest.raises(FilesError) as e:
        gateway_url("http://127.0.0.1:9")
    assert e.value.code == "UNREACHABLE"


def test_a_photo_goes_up_and_comes_back_and_is_deleted(cell: Cell) -> None:
    photo = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    f = helper(cell)
    f.put("photos/cat.png", photo, content_type="image/png")
    got = f.get("photos/cat.png")
    f.delete("photos/cat.png")
    assert got == photo
    assert cell.objects == {}
    assert [op for op, _, _ in cell.asked] == ["put", "get", "delete"]
    assert json.loads(cell.asked[0][2]) == {"name": "photos/cat.png", "content_type": "image/png"}
    for _, headers, _ in cell.asked:
        assert headers["authorization"].startswith("Bearer ")
    assert cell.audiences == [cell.url]
    assert (cell.sent_headers[0]["x-check"], cell.sent_headers[0]["content-type"]) == (
        "signed",
        "image/png",
    )


def test_a_get_link_is_handed_out_for_a_browser(cell: Cell) -> None:
    cell.objects["report.html"] = (b"<p>hi</p>", "text/html")
    link = helper(cell).link("get", "report.html")
    assert (link["method"], link["url"]) == ("GET", f"{cell.url}/storage/report.html")


def test_the_data_gateway_is_asked_once_more_while_it_starts(cell: Cell) -> None:
    cell.gateway_answers = [(503, b"starting"), refusal("FILES_UNAVAILABLE", 503)]
    with pytest.raises(FilesError) as e:
        helper(cell).put("a.txt", b"x")
    assert (e.value.code, e.value.status) == ("FILES_UNAVAILABLE", 503)
    assert len(cell.asked) == 2
    cell.asked.clear()
    cell.gateway_answers = [(502, b"")]
    helper(cell).put("a.txt", b"x")
    assert len(cell.asked) == 2
    cell.asked.clear()
    cell.gateway_answers = [(504, b""), (502, b"")]
    with pytest.raises(FilesError) as e:
        helper(cell).delete("a.txt")
    assert (e.value.code, e.value.status, len(cell.asked)) == ("UNAVAILABLE", 502, 2)


def test_a_refusal_is_not_asked_again(cell: Cell) -> None:
    cell.gateway_answers = [refusal("APP_NOT_ACTIVE", 403)]
    with pytest.raises(FilesError) as e:
        helper(cell).put("a.txt", b"x")
    assert (e.value.code, e.value.status, len(cell.asked)) == ("APP_NOT_ACTIVE", 403, 1)
    with pytest.raises(FilesError) as e:
        helper(cell).get("missing.txt")
    assert e.value.code == "FILE_NOT_FOUND"


def test_a_gateway_that_cannot_be_reached_is_an_error(cell: Cell) -> None:
    with pytest.raises(FilesError) as e:
        Files(url="http://127.0.0.1:9", metadata=cell.url).put("a.txt", b"x")
    assert e.value.code == "UNREACHABLE"


def test_storage_that_refuses_the_transfer_is_an_error(cell: Cell) -> None:
    cell.storage_status = 400
    with pytest.raises(FilesError) as e:
        helper(cell).put("big.bin", b"x" * 10)
    assert (e.value.code, e.value.status) == ("STORAGE_400", 400)


def test_without_a_url_the_metadata_server_names_the_gateway(
    cell: Cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    def nowhere(method: str, url: str, *args: object) -> tuple[int, bytes]:
        called.append(url)
        raise OSError

    monkeypatch.delenv(files.URL_VARIABLE, raising=False)
    monkeypatch.setattr(files, "_send", nowhere)
    with pytest.raises(FilesError) as e:
        Files(metadata=cell.url).put("a.txt", b"x")
    gateway = f"https://ssc-datagw-{NUMBER}.{REGION}.run.app"
    assert e.value.code == "UNREACHABLE"
    assert called == [f"{gateway}/v1/files/put"] * 2
    assert cell.audiences == [gateway]


def test_the_variable_names_the_gateway_for_the_module_functions(
    cell: Cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(files.URL_VARIABLE, cell.url + "/")
    monkeypatch.setattr(files, "_default", Files(metadata=cell.url))
    files.put("notes/a.txt", b"hello", content_type="text/plain")
    assert files.get("notes/a.txt") == b"hello"
    assert files.link("get", "notes/a.txt")["method"] == "GET"
    files.delete("notes/a.txt")
    assert cell.objects == {}
    assert cell.audiences == [cell.url]
