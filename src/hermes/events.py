"""Single source of truth for the SSE event envelope streamed by /api/chat.

Every event the chat stream emits shares one envelope::

    {"event": "<name>", "version": <int>, "data": {...}}

On the wire the SSE ``event:`` line mirrors the envelope's ``event`` field
(see :func:`to_sse`), so clients can switch on either; the JSON body is always
the full envelope. These Pydantic models are the *only* place event shapes are
defined — route handlers construct them and the frontend consumes the matching
TypeScript types generated from the OpenAPI schema. Do not hand-write parallel
event structures anywhere else.

Forward-compatibility contract (mirrored by the frontend):

- Clients ignore envelopes whose ``event`` they don't recognise.
- Adding a new *optional* field to a ``data`` model does **not** bump
  ``version``.
- Removing a field or changing the meaning of an existing one **does** bump the
  affected event's ``version``.

Plans 09 (approvals) and 10 (reasoning/subagents) add new event types here and
reuse this envelope rather than inventing their own.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, RootModel

# --- per-event data payloads ------------------------------------------------


class SessionData(BaseModel):
    conversation_id: int


class RunData(BaseModel):
    run_id: str


class TextData(BaseModel):
    content: str


class ToolCallData(BaseModel):
    """A tool invocation has started. Emitted before the tool runs."""

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["running"] = "running"


class ToolResultData(BaseModel):
    """A tool invocation finished. ``result`` is set on success, ``error`` on
    failure; exactly one is non-null for a given ``status``."""

    call_id: str
    status: Literal["success", "error"]
    result: str | None = None
    error: str | None = None


class ApprovalRequiredData(BaseModel):
    """A risky tool call is paused awaiting the user's decision. The agent
    blocks until a decision arrives via ``POST /api/approvals/{approval_id}``;
    until then the stream stays open (heartbeats only). ``call_id`` ties this
    back to the tool call that would run on approval; ``reason`` is the
    human-readable risk explanation shown on the card."""

    approval_id: str
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str


class ReasoningData(BaseModel):
    """An incremental chunk of the model's reasoning / "thinking" output.

    Streamed like ``text`` (one event per delta, the client concatenates),
    but kept distinct so the UI can render it in a separate, collapsible card
    instead of mixing it into the answer. Only emitted when the provider
    actually exposes reasoning (e.g. an OpenAI-compatible ``reasoning_content``
    delta); a provider that emits none leaves the normal chat untouched."""

    content: str


class SubagentStartData(BaseModel):
    """A subagent began working. ``subagent_id`` groups all events for one
    subagent run; ``name`` is its human label and ``prompt`` the optional task
    it was handed. Holzi does not orchestrate subagents yet — these types
    define the wire contract so a future orchestrator only has to emit them
    and the UI already groups + renders them."""

    subagent_id: str
    name: str
    prompt: str | None = None


class SubagentTextData(BaseModel):
    """An incremental output chunk from a running subagent, grouped by
    ``subagent_id`` (mirrors ``text`` but namespaced to a subagent)."""

    subagent_id: str
    content: str


class SubagentDoneData(BaseModel):
    """A subagent finished. ``result`` is set on success, ``error`` on
    failure; ``status`` says which."""

    subagent_id: str
    status: Literal["success", "error"] = "success"
    result: str | None = None
    error: str | None = None


class ErrorData(BaseModel):
    code: str
    status_code: int
    message: str


class EmptyData(BaseModel):
    """Payload for events that carry no data (``done``, ``cancelled``)."""


# --- envelopes --------------------------------------------------------------


class SessionEvent(BaseModel):
    event: Literal["session"] = "session"
    version: int = 1
    data: SessionData


class RunEvent(BaseModel):
    event: Literal["run"] = "run"
    version: int = 1
    data: RunData


class TextEvent(BaseModel):
    event: Literal["text"] = "text"
    version: int = 1
    data: TextData


class ToolCallEvent(BaseModel):
    event: Literal["tool_call"] = "tool_call"
    version: int = 1
    data: ToolCallData


class ToolResultEvent(BaseModel):
    event: Literal["tool_result"] = "tool_result"
    version: int = 1
    data: ToolResultData


class ApprovalRequiredEvent(BaseModel):
    event: Literal["approval_required"] = "approval_required"
    version: int = 1
    data: ApprovalRequiredData


class ReasoningEvent(BaseModel):
    event: Literal["reasoning"] = "reasoning"
    version: int = 1
    data: ReasoningData


class SubagentStartEvent(BaseModel):
    event: Literal["subagent_start"] = "subagent_start"
    version: int = 1
    data: SubagentStartData


class SubagentTextEvent(BaseModel):
    event: Literal["subagent_text"] = "subagent_text"
    version: int = 1
    data: SubagentTextData


class SubagentDoneEvent(BaseModel):
    event: Literal["subagent_done"] = "subagent_done"
    version: int = 1
    data: SubagentDoneData


class DoneEvent(BaseModel):
    event: Literal["done"] = "done"
    version: int = 1
    data: EmptyData = Field(default_factory=EmptyData)


class CancelledEvent(BaseModel):
    event: Literal["cancelled"] = "cancelled"
    version: int = 1
    data: EmptyData = Field(default_factory=EmptyData)


class ErrorEvent(BaseModel):
    event: Literal["error"] = "error"
    version: int = 1
    data: ErrorData


ChatStreamEvent = Annotated[
    SessionEvent
    | RunEvent
    | TextEvent
    | ToolCallEvent
    | ToolResultEvent
    | ApprovalRequiredEvent
    | ReasoningEvent
    | SubagentStartEvent
    | SubagentTextEvent
    | SubagentDoneEvent
    | DoneEvent
    | CancelledEvent
    | ErrorEvent,
    Field(discriminator="event"),
]


class ChatStreamEnvelope(RootModel[ChatStreamEvent]):
    """Documentation wrapper so the discriminated union surfaces as a single
    named component in the OpenAPI schema (and thus the generated TS types).
    Referenced from /api/chat's 200 response; never instantiated at runtime."""


def to_sse(event: BaseModel) -> bytes:
    """Serialise one envelope to an SSE block, mirroring ``event`` onto the
    SSE ``event:`` line."""
    name = event.event  # type: ignore[attr-defined]
    return f"event: {name}\ndata: {event.model_dump_json()}\n\n".encode()
