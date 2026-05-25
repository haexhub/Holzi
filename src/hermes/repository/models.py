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


@dataclass(frozen=True, slots=True)
class LlmCredential:
    """A row from `llm_credentials`. Ciphertext columns stay raw — callers
    decide whether and when to decrypt (route handlers never decrypt; the
    agent loop and the proxy resolver do)."""

    id: int
    provider: str
    mode: str
    display_name: str
    base_url: str | None
    model: str | None
    is_active: bool
    api_key_iv: str | None
    api_key_tag: str | None
    api_key_data: str | None
    oauth_status: str | None
    oauth_authorized_at: int | None
    oauth_iv: str | None
    oauth_tag: str | None
    oauth_data: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class MessengerAccount:
    """A row from `messenger_accounts`. `bot_token_*` ciphertext columns
    are only populated for telegram rows; signal rows leave them NULL and
    keep their state in the signal-cli volume."""

    id: int
    provider: str
    is_active: bool
    phone_number: str | None
    bot_username: str | None
    bot_token_iv: str | None
    bot_token_tag: str | None
    bot_token_data: str | None
    allowed_chat_ids: str | None
    created_at: int
    updated_at: int
