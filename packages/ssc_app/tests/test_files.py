"""The Python file helper (SSC-046) against one local server that stands in for the metadata
server, the data gateway and Cloud Storage, so a link it hands out points back at itself."""

import base64
import http.client
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
    token_status: int | None = None
    """The metadata server answers this instead of an ID token."""
    storage_cut: bool = False
    """Cloud Storage's download ends before its ``content-length``."""
    max_bytes: int = 25 * 1024 * 1024
    lose_delete_answer: int | None = None
    """Delete the file, then answer this status instead of 200, once (a lost response)."""


CUT = -1
"""As a gateway answer's status: a 200 whose body ends before its ``content-length``."""


def refusal(code: str, status: int = 404) -> tuple[int, bytes]:
    body = {"error": {"code": code, "message": "fixed", "stage": "files", "fix_owner": "app"}}
    return status, json.dumps(body).encode()


@pytest.fixture
def cell() -> Iterator[Cell]:
    state = Cell()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self, status: int, body: bytes = b"", **headers: str) -> None:
            cut = status == CUT
            self.send_response(200 if cut else status)
            for k, v in headers.items():
                self.send_header(k.replace("_", "-"), v)
            self.send_header("content-length", str(len(body) + (100 if cut else 0)))
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
            elif url.path == IDENTITY_PATH and state.token_status is not None:
                self._answer(state.token_status)
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
                if found and state.lose_delete_answer is not None:
                    self._answer(state.lose_delete_answer, b"upstream connect error")
                    state.lose_delete_answer = None
                    return
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
            if op == "put":
                link["max_bytes"] = state.max_bytes
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
            status = CUT if state.storage_cut else 200
            self._answer(status, data, content_type=kind, content_disposition="attachment")

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
    assert (e.value.code, e.value.status, len(cell.asked)) == ("BAD_GATEWAY_RESPONSE", 502, 2)


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


def test_a_retried_delete_that_finds_nothing_succeeds(cell: Cell) -> None:
    """The data gateway deleted the file but its answer was lost (503), so the retry finds
    nothing: delete is idempotent and returns normally."""
    cell.objects["a.txt"] = (b"x", "text/plain")
    cell.lose_delete_answer = 503
    helper(cell).delete("a.txt")
    assert cell.objects == {}
    assert [op for op, _, _ in cell.asked] == ["delete", "delete"]


def test_a_delete_of_a_file_that_never_existed_is_file_not_found(cell: Cell) -> None:
    with pytest.raises(FilesError) as e:
        helper(cell).delete("never.txt")
    assert (e.value.code, e.value.status, len(cell.asked)) == ("FILE_NOT_FOUND", 404, 1)


def test_a_retried_get_that_finds_nothing_is_still_file_not_found(cell: Cell) -> None:
    cell.gateway_answers = [(503, b"starting")]
    with pytest.raises(FilesError) as e:
        helper(cell).get("never.txt")
    assert (e.value.code, len(cell.asked)) == ("FILE_NOT_FOUND", 2)


def test_a_metadata_server_that_refuses_the_token_is_unreachable(cell: Cell) -> None:
    cell.token_status = 403
    with pytest.raises(FilesError) as e:
        helper(cell).put("a.txt", b"x")
    assert (e.value.code, cell.asked) == ("UNREACHABLE", [])
    assert "403" in str(e.value)
    with pytest.raises(FilesError) as e:
        helper(cell).delete("a.txt")
    assert e.value.code == "UNREACHABLE"


def test_an_answer_cut_short_is_asked_once_more_then_unreachable(cell: Cell) -> None:
    cell.gateway_answers = [(CUT, b'{"url')]
    helper(cell).put("a.txt", b"x")
    assert len(cell.asked) == 2
    cell.asked.clear()
    cell.gateway_answers = [(CUT, b'{"url'), (CUT, b'{"url')]
    with pytest.raises(FilesError) as e:
        helper(cell).put("a.txt", b"x")
    assert (e.value.code, len(cell.asked)) == ("UNREACHABLE", 2)


def test_a_200_that_is_not_json_is_a_bad_gateway_response(cell: Cell) -> None:
    cell.gateway_answers = [(200, b"<html>hello</html>")]
    with pytest.raises(FilesError) as e:
        helper(cell).put("a.txt", b"x")
    assert (e.value.code, e.value.status, len(cell.asked)) == ("BAD_GATEWAY_RESPONSE", 200, 1)


def test_a_download_cut_short_is_unreachable(cell: Cell) -> None:
    cell.objects["a.txt"] = (b"hello", "text/plain")
    cell.storage_cut = True
    with pytest.raises(FilesError) as e:
        helper(cell).get("a.txt")
    assert e.value.code == "UNREACHABLE"


def test_a_region_cut_short_is_unreachable(cell: Cell, monkeypatch: pytest.MonkeyPatch) -> None:
    def short(*args: object, **kwargs: object) -> object:
        raise http.client.IncompleteRead(b"proj")

    monkeypatch.setattr(files.urllib.request, "urlopen", short)
    with pytest.raises(FilesError) as e:
        gateway_url(cell.url)
    assert e.value.code == "UNREACHABLE"


def test_a_file_over_the_link_s_limit_is_refused_before_it_is_sent(cell: Cell) -> None:
    cell.max_bytes = 4
    with pytest.raises(FilesError) as e:
        helper(cell).put("a.txt", b"12345")
    assert (e.value.code, cell.sent_headers, cell.objects) == ("FILE_TOO_LARGE", [], {})
    helper(cell).put("a.txt", b"1234")
    assert cell.objects["a.txt"][0] == b"1234"
