from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Conversation:
    id: int
    channel: str
    external_id: str | None
    title: str | None
    started_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class Message:
    id: int
    conversation_id: int
    role: str
    content: str
    ts: int
    meta_json: str | None


@dataclass(frozen=True, slots=True)
class Note:
    id: int
    key: str
    content: str
    tags: str | None
    updated_at: int
