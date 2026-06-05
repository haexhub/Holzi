"""Unit tests for _classify_chat_error and _sanitize_upstream_message."""
import json
import pytest
import httpx

from hermes.routes.api import _classify_chat_error, _sanitize_upstream_message


def _make_status_error(status: int, body: bytes = b"") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://upstream/v1/chat/completions")
    resp = httpx.Response(status, content=body, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


def test_classify_429_returns_rate_limited_code():
    body = json.dumps({"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}}).encode()
    exc = _make_status_error(429, body)
    code, status_code, message = _classify_chat_error(exc)
    assert code == "upstream_rate_limited"
    assert status_code == 429
    assert "Rate limit exceeded" in message


def test_classify_503_returns_upstream_http_error():
    body = json.dumps({"error": {"message": "Service unavailable", "type": "overloaded_error"}}).encode()
    exc = _make_status_error(503, body)
    code, status_code, message = _classify_chat_error(exc)
    assert code == "upstream_http_error"
    assert status_code == 503
    assert "Service unavailable" in message


def test_classify_http_error_with_empty_body():
    exc = _make_status_error(500, b"")
    code, status_code, message = _classify_chat_error(exc)
    assert code == "upstream_http_error"
    assert status_code == 500
    assert "HTTP 500" in message


def test_sanitize_redacts_api_key_in_message():
    body = json.dumps({"error": {"message": "Invalid key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}}).encode()
    exc = _make_status_error(401, body)
    _, _, message = _classify_chat_error(exc)
    assert "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in message
    assert "[REDACTED]" in message


def test_sanitize_upstream_message_openai_shape():
    body = json.dumps({"error": {"message": "You exceeded your current quota", "type": "insufficient_quota"}}).encode()
    msg = _sanitize_upstream_message(body, 429)
    assert msg == "You exceeded your current quota"


def test_sanitize_upstream_message_non_json_falls_back():
    msg = _sanitize_upstream_message(b"<html>Bad Gateway</html>", 502)
    assert msg == "HTTP 502"


def test_sanitize_upstream_message_empty_body():
    msg = _sanitize_upstream_message(b"", 429)
    assert msg == "HTTP 429"


def test_classify_timeout():
    exc = httpx.TimeoutException("timed out")
    code, status_code, message = _classify_chat_error(exc)
    assert code == "upstream_timeout"
    assert status_code == 504


def test_classify_request_error():
    exc = httpx.ConnectError("connection refused")
    code, status_code, message = _classify_chat_error(exc)
    assert code == "upstream_unreachable"
    assert status_code == 502
