"""The Python data helper (SSC-052) against one local server that stands in for the metadata
server and the data gateway."""

import base64
import http.server
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest

from ssc_app import data
from ssc_app.data import Data, DataError
from ssc_app.files import REGION_PATH
from ssc_app.workload import IDENTITY_PATH

NUMBER, REGION = "123456789012", "us-central1"
RESULT = {
    "columns": [{"name": "total", "type": "decimal", "db_type": "numeric"}],
    "rows": [["12.50"], ["7.00"]],
    "row_count": 2,
    "truncated": True,
    "truncated_reason": "max_rows",
    "snapshot_version": 4,
    "request_id": "req-1",
    "elapsed_ms": 3,
}


def _token(audience: str) -> str:
    def part(doc: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(doc).encode()).rstrip(b"=").decode()

    claims = {"aud": audience, "exp": 4_000_000_000, "email": "ssc-a-x@p.iam.gserviceaccount.com"}
    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2ln"


@dataclass
class Cell:
    """What the stand-in was asked and what it answers next."""

    url: str = ""
    audiences: list[str] = field(default_factory=list[str])
    asked: list[tuple[str, dict[str, str], dict[str, object]]] = field(
        default_factory=list[tuple[str, dict[str, str], dict[str, object]]]
    )
    answers: list[tuple[int, bytes]] = field(default_factory=list[tuple[int, bytes]])


def refusal(code: str, status: int, **extra: str) -> tuple[int, bytes]:
    error = {"code": code, "stage": "execute", "message": "fixed", "fix_owner": "app", **extra}
    return status, json.dumps({"error": error, "request_id": "req-1"}).encode()


@pytest.fixture
def cell() -> Iterator[Cell]:
    state = Cell()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self, status: int, body: bytes = b"") -> None:
            self.send_response(status)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path == REGION_PATH:
                self._answer(200, f"projects/{NUMBER}/regions/{REGION}".encode())
            elif url.path == IDENTITY_PATH:
                audience = parse_qs(url.query)["audience"][0]
                state.audiences.append(audience)
                self._answer(200, _token(audience).encode())
            else:
                self._answer(404)

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.asked.append((self.path, headers, body))
            if state.answers:
                self._answer(*state.answers.pop(0))
            else:
                self._answer(200, json.dumps(RESULT).encode())

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def helper(cell: Cell) -> Data:
    return Data(url=cell.url, metadata=cell.url)


def test_a_query_carries_the_statement_the_parameters_and_the_workload_token(cell: Cell) -> None:
    result = helper(cell).query("finance", "select total from t where y = $1", [2026])
    assert result.rows == [["12.50"], ["7.00"]]
    assert (result.row_count, result.truncated, result.truncated_reason) == (2, True, "max_rows")
    assert result.columns[0]["name"] == "total"
    assert result.request_id == "req-1"
    ((path, headers, body),) = cell.asked
    assert path == "/v1/connections/finance/query"
    assert body == {"sql": "select total from t where y = $1", "params": [2026]}
    assert headers["authorization"].startswith("Bearer ")
    assert "x-ssc-identity" not in headers
    assert cell.audiences == [cell.url]


def test_the_asks_and_the_identity_note_are_sent_when_given(cell: Cell) -> None:
    helper(cell).query(
        "finance",
        "select 1",
        max_rows=10,
        max_bytes=2000,
        timeout_ms=500,
        identity="note.jwt.here",
    )
    ((_, headers, body),) = cell.asked
    assert body == {
        "sql": "select 1",
        "params": [],
        "max_rows": 10,
        "max_bytes": 2000,
        "timeout_ms": 500,
    }
    assert headers["x-ssc-identity"] == "note.jwt.here"


def test_a_zero_ask_is_sent(cell: Cell) -> None:
    helper(cell).query("finance", "select 1", max_rows=0)
    assert cell.asked[0][2]["max_rows"] == 0


def test_a_refusal_names_its_code_and_is_not_asked_again(cell: Cell) -> None:
    cell.answers = [refusal("CONNECTION_NOT_GRANTED", 403)]
    with pytest.raises(DataError) as e:
        helper(cell).query("finance", "select 1")
    assert (e.value.code, e.value.status, len(cell.asked)) == ("CONNECTION_NOT_GRANTED", 403, 1)
    cell.answers = [refusal("QUERY_FAILED", 422, sqlstate="42P01")]
    with pytest.raises(DataError) as e:
        helper(cell).query("finance", "select * from nowhere")
    assert (e.value.code, e.value.sqlstate) == ("QUERY_FAILED", "42P01")


def test_the_data_gateway_is_asked_once_more_while_it_starts(cell: Cell) -> None:
    cell.answers = [(503, b"starting")]
    assert helper(cell).query("finance", "select 1").row_count == 2
    assert len(cell.asked) == 2
    cell.asked.clear()
    cell.answers = [(502, b""), (504, b"")]
    with pytest.raises(DataError) as e:
        helper(cell).query("finance", "select 1")
    assert (e.value.code, e.value.status, len(cell.asked)) == ("UNAVAILABLE", 504, 2)


def test_a_gateway_that_cannot_be_reached_is_an_error(cell: Cell) -> None:
    with pytest.raises(DataError) as e:
        Data(url="http://127.0.0.1:9", metadata=cell.url).query("finance", "select 1")
    assert e.value.code == "UNREACHABLE"


def test_a_metadata_server_that_cannot_be_reached_is_an_error() -> None:
    with pytest.raises(DataError) as e:
        Data(metadata="http://127.0.0.1:9").query("finance", "select 1")
    assert e.value.code == "UNREACHABLE"


def test_without_a_url_the_metadata_server_names_the_gateway(
    cell: Cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    def nowhere(url: str, *args: object) -> tuple[int, bytes]:
        called.append(url)
        raise OSError

    monkeypatch.delenv(data.URL_VARIABLE, raising=False)
    monkeypatch.setattr(data, "_send", nowhere)
    with pytest.raises(DataError) as e:
        Data(metadata=cell.url).query("finance", "select 1")
    gateway = f"https://ssc-datagw-{NUMBER}.{REGION}.run.app"
    assert e.value.code == "UNREACHABLE"
    assert called == [f"{gateway}/v1/connections/finance/query"] * 2
    assert cell.audiences == [gateway]


def test_the_variable_names_the_gateway_for_the_module_function(
    cell: Cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(data.URL_VARIABLE, cell.url + "/")
    monkeypatch.setattr(data, "_default", Data(metadata=cell.url))
    assert data.query("finance", "select 1", [1], max_rows=5).row_count == 2
    assert cell.asked[0][2]["max_rows"] == 5
    assert cell.audiences == [cell.url]
