"""SSC-011: the error catalogue is complete, fixed-text, and renders one problem shape."""

import pytest
from pydantic import ValidationError

from ssc_contracts.errors import (
    CATALOGUE,
    PROBLEM_MEDIA_TYPE,
    PROBLEM_MEMBERS,
    PROBLEM_TYPE_BASE,
    ErrorCode,
    Problem,
    problem_type,
)


def test_every_code_has_a_catalogue_entry() -> None:
    assert set(CATALOGUE) == set(ErrorCode)


def test_catalogue_text_is_fixed_and_placeholder_free() -> None:
    for code, entry in CATALOGUE.items():
        assert 400 <= entry.status <= 599, code
        for sentence in (entry.title, entry.detail):
            assert sentence, code
            assert "{" not in sentence and "%" not in sentence, code
            assert sentence[0].isupper() and sentence.endswith("."), code


def test_codes_are_upper_snake_and_unique() -> None:
    values = [c.value for c in ErrorCode]
    assert len(values) == len(set(values))
    for value in values:
        assert value.replace("_", "").isalpha() and value.isupper(), value


def test_problem_type_is_a_url_under_the_documented_base() -> None:
    assert problem_type(ErrorCode.NOT_FOUND) == f"{PROBLEM_TYPE_BASE}NOT_FOUND"
    assert PROBLEM_TYPE_BASE.startswith("https://") and PROBLEM_TYPE_BASE.endswith("/")


def test_problem_shape_is_closed() -> None:
    entry = CATALOGUE[ErrorCode.NOT_FOUND]
    p = Problem(
        type=problem_type(ErrorCode.NOT_FOUND),
        title=entry.title,
        status=entry.status,
        detail=entry.detail,
        instance="/v1/apps/app_x",
        code=ErrorCode.NOT_FOUND,
        request_id="abc",
    )
    assert tuple(p.model_dump()) == PROBLEM_MEMBERS
    assert PROBLEM_MEDIA_TYPE == "application/problem+json"
    with pytest.raises(ValidationError):
        Problem.model_validate({**p.model_dump(), "evidence": {"leak": 1}})
    with pytest.raises(ValidationError):
        Problem.model_validate({**p.model_dump(), "status": 200})
