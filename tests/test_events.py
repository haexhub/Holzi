import json

from hermes.events import (
    ChatStreamEnvelope,
    SessionData,
    SessionEvent,
    ToolCallData,
    ToolCallEvent,
    ToolResultData,
    ToolResultEvent,
    to_sse,
)


def test_to_sse_mirrors_event_name_onto_sse_event_line() -> None:
    block = to_sse(SessionEvent(data=SessionData(conversation_id=7))).decode()
    lines = block.strip().split("\n")
    assert lines[0] == "event: session"
    payload = json.loads(lines[1][len("data: ") :])
    assert payload == {
        "event": "session",
        "version": 1,
        "data": {"conversation_id": 7},
    }


def test_tool_call_event_defaults_status_running() -> None:
    evt = ToolCallEvent(data=ToolCallData(call_id="c1", name="notes.find"))
    dumped = evt.model_dump()
    assert dumped["event"] == "tool_call"
    assert dumped["data"]["status"] == "running"
    assert dumped["data"]["arguments"] == {}


def test_tool_result_event_carries_status_and_result() -> None:
    evt = ToolResultEvent(
        data=ToolResultData(call_id="c1", status="success", result="ok")
    )
    dumped = evt.model_dump()
    assert dumped["data"]["status"] == "success"
    assert dumped["data"]["result"] == "ok"
    assert dumped["data"]["error"] is None


def test_envelope_round_trips_discriminated_union() -> None:
    raw = {
        "event": "tool_result",
        "version": 1,
        "data": {"call_id": "c1", "status": "error", "error": "boom"},
    }
    parsed = ChatStreamEnvelope.model_validate(raw)
    assert isinstance(parsed.root, ToolResultEvent)
    assert parsed.root.data.error == "boom"
