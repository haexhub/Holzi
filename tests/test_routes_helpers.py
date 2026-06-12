"""Tests for hermes.routes._helpers."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hermes.errors import ErrorCode
from hermes.routes._helpers import http_error, require_sandbox_manager, validate_limit


def _fake_request(sandbox_manager):
    state = SimpleNamespace(sandbox_manager=sandbox_manager)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_require_sandbox_manager_returns_manager_when_present():
    mgr = object()
    req = _fake_request(mgr)
    assert require_sandbox_manager(req) is mgr


def test_require_sandbox_manager_raises_503_when_missing():
    req = _fake_request(None)
    with pytest.raises(HTTPException) as exc:
        require_sandbox_manager(req)
    assert exc.value.status_code == 503
    assert exc.value.detail == ErrorCode.SANDBOX_NOT_CONFIGURED.value


def test_validate_limit_returns_value_when_in_range():
    assert validate_limit(50) == 50


def test_validate_limit_accepts_explicit_max_at_boundary():
    assert validate_limit(7, max_limit=7) == 7


def test_validate_limit_rejects_zero():
    with pytest.raises(HTTPException) as exc:
        validate_limit(0)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == ErrorCode.REQUEST_LIMIT_OUT_OF_RANGE.value
    assert exc.value.detail["params"] == {"min": 1, "max": 500}


def test_validate_limit_rejects_negative():
    with pytest.raises(HTTPException):
        validate_limit(-1)


def test_validate_limit_rejects_over_max():
    with pytest.raises(HTTPException) as exc:
        validate_limit(501)
    assert exc.value.detail["params"]["max"] == 500


def test_validate_limit_respects_custom_max():
    with pytest.raises(HTTPException) as exc:
        validate_limit(11, max_limit=10)
    assert exc.value.detail["params"] == {"min": 1, "max": 10}


def test_http_error_returns_exception_with_dict_detail_when_params_given():
    err = http_error(404, ErrorCode.PERSONA_NOT_FOUND, params={"id": 7})
    assert isinstance(err, HTTPException)
    assert err.status_code == 404
    assert err.detail == {"code": ErrorCode.PERSONA_NOT_FOUND.value, "params": {"id": 7}}


def test_http_error_returns_bare_string_detail_when_no_params():
    err = http_error(422, ErrorCode.PERSONA_DEFAULT_DEMOTE)
    assert err.detail == ErrorCode.PERSONA_DEFAULT_DEMOTE.value


def test_http_error_accepts_empty_params_dict_as_explicit_dict_shape():
    err = http_error(422, ErrorCode.PERSONA_FRAGMENTS_ALL_EMPTY, params={})
    assert err.detail == {"code": ErrorCode.PERSONA_FRAGMENTS_ALL_EMPTY.value, "params": {}}


def test_http_error_chains_with_from_exc():
    cause = ValueError("boom")
    try:
        raise http_error(409, ErrorCode.PERSONA_NAME_CONFLICT, params={"name": "x"}) from cause
    except HTTPException as exc:
        assert exc.__cause__ is cause
    else:
        pytest.fail("HTTPException not raised")
