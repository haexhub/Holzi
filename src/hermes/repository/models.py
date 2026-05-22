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


@dataclass(frozen=True, slots=True)
class Reminder:
    id: int
    due_at: int
    message: str
    channel: str
    fired_at: int | None
    created_at: int


@dataclass(frozen=True, slots=True)
class Todo:
    id: int
    content: str
    tags: str | None
    done_at: int | None
    created_at: int
