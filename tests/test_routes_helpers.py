"""Tests for hermes.routes._helpers."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hermes.errors import ErrorCode
from hermes.routes._helpers import require_sandbox_manager, validate_limit


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
