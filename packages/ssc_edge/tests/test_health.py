from fastapi.testclient import TestClient

from ssc_edge.main import app


def test_healthz():
    assert TestClient(app).get("/healthz").json() == {"status": "ok"}
