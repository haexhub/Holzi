"""Shared SSE / upstream-mock helpers for the /api/chat test family.

Used by test_api_chat.py, test_api_chat_approvals.py, and
test_api_chat_message_ops.py. Other test modules have their own variants
(e.g. test_api_runs.py emits `finish_reason="stop"` + a `usage` chunk;
test_meta_tools_redaction.py has a stripped-down version) — those are
intentionally divergent and stay local.

Public names (kept without leading underscore for cross-module clarity):
- `assistant_oneshot(content)` -> a canned non-streaming chat completion
- `install_upstream_responses(responses)` -> swaps app.state.upstream for
  an httpx.MockTransport that replays the given canned responses as SSE.
  Returns the list of request bodies, mutated as calls arrive.
- `to_sse_stream(payload)` -> turns a canned non-streaming response into
  the OpenAI-style SSE bytes the agent loop reads. Handles content delta
  AND tool_calls delta.
- `parse_sse(body)` -> list of `(event_name, data_payload)` extracted
  from a `event: …\\ndata: {…}\\n\\n`-framed body (unwraps the shared
  envelope `{event, version, data}`).
- `parse_sse_envelopes(body)` -> the same parse but returns the full
  envelope dict for tests that assert on envelope shape.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from hermes.main import app


def assistant_oneshot(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "model": "claude-opus-4-7",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def install_upstream_responses(
    responses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Install a MockTransport on app.state.upstream and return the list of
    request bodies that were sent upstream (mutated as calls arrive)."""
    iter_resp = iter(responses)
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        try:
            payload = next(iter_resp)
        except StopIteration as exc:
            raise AssertionError(
                "upstream called more times than expected"
            ) from exc
        body = to_sse_stream(payload)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(body),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )
    return seen


def to_sse_stream(payload: dict[str, Any]) -> bytes:
    msg = payload["choices"][0]["message"]
    content = msg.get("content")
    tool_calls = msg.get("tool_calls") or []
    out = b""
    if content:
        chunk = {
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": None}
            ]
        }
        out += f"data: {json.dumps(chunk)}\n\n".encode()
    if tool_calls:
        delta_tcs = [
            {
                "index": i,
                "id": tc["id"],
                "type": tc.get("type", "function"),
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                },
            }
            for i, tc in enumerate(tool_calls)
        ]
        chunk = {
            "choices": [
                {"index": 0, "delta": {"tool_calls": delta_tcs}, "finish_reason": None}
            ]
        }
        out += f"data: {json.dumps(chunk)}\n\n".encode()
    out += b"data: [DONE]\n\n"
    return out


def parse_sse_envelopes(
    body: bytes,
) -> list[tuple[str, dict[str, Any]]]:
    """Parse SSE blocks into `(sse_event_line, full_envelope)` pairs."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.split(b"\n\n"):
        if not block.strip():
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.split(b"\n"):
            line = line.strip()
            if line.startswith(b"event: "):
                event = line[len(b"event: ") :].decode()
            elif line.startswith(b"data: "):
                data_lines.append(line[len(b"data: ") :].decode())
        if event:
            data = json.loads("\n".join(data_lines)) if data_lines else {}
            events.append((event, data))
    return events


def parse_sse(body: bytes) -> list[tuple[str, dict[str, Any]]]:
    """Parse a series of ``event: name\\ndata: {...}\\n\\n`` blocks.

    Every block carries the shared envelope ``{event, version, data}``; this
    helper unwraps it and returns ``(event_name, data_payload)`` so tests
    assert against the inner payload. ``parse_sse_envelopes`` exposes the
    raw envelope for tests that check the envelope contract itself.
    """
    return [(name, env.get("data", {})) for name, env in parse_sse_envelopes(body)]
