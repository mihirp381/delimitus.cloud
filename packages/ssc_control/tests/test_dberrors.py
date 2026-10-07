"""Database errors become catalogue codes (``api.dberrors``); a lost race is retryable."""

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from ssc_contracts.errors import PROBLEM_MEDIA_TYPE, ErrorCode
from ssc_control.api import problems
from ssc_control.api.dberrors import classify


def _wrapped(orig: psycopg.Error) -> DBAPIError:
    return DBAPIError("update ssc.app set ...", {}, orig)


@pytest.mark.parametrize(
    "orig", [psycopg.errors.SerializationFailure(), psycopg.errors.DeadlockDetected()]
)
def test_a_serialization_failure_or_deadlock_is_retryable(orig: psycopg.Error) -> None:
    code, evidence = classify(_wrapped(orig))
    assert code is ErrorCode.TRANSIENT_CONFLICT
    assert evidence["sqlstate"] == orig.sqlstate

    app = FastAPI()
    problems.install(app)

    @app.post("/race")
    def race() -> None:
        raise _wrapped(orig)

    r = TestClient(app).post("/race")
    assert r.status_code == 503
    assert r.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert r.headers["retry-after"] == "1"
    assert r.json()["code"] == "TRANSIENT_CONFLICT"
    assert str(orig.sqlstate) not in r.text


def test_other_errors_carry_no_retry_after() -> None:
    app = FastAPI()
    problems.install(app)

    @app.post("/bug")
    def bug() -> None:
        raise _wrapped(psycopg.errors.InsufficientPrivilege())

    r = TestClient(app).post("/bug")
    assert r.status_code == 500
    assert "retry-after" not in r.headers
