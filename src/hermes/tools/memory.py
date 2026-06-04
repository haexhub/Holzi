import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import Tool
from hermes.repository import conversations, messages, notes


def build_memory_tools(db: AsyncEngine) -> list[Tool]:
    return [
        _recall_memory(db),
        _list_conversations(db),
        _get_conversation(db),
        _save_note(db),
        _get_note(db),
        _find_notes(db),
    ]


# ----------------------------------------------------------------------------
def _recall_memory(db: AsyncEngine) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        query = str(args.get("query", ""))
        limit = int(args.get("limit", 10))

        msg_hits = await messages.fts_search(db, query=query, limit=limit)
        note_hits = await notes.find(db, query=query, limit=limit)

        return json.dumps(
            {
                "messages": [
                    {
                        "id": m.id,
                        "conversation_id": m.conversation_id,
                        "role": m.role,
                        "content": m.content,
                        "ts": m.ts,
                    }
                    for m in msg_hits
                ],
                "notes": [
                    {"id": n.id, "key": n.key, "content": n.content, "tags": n.tags}
                    for n in note_hits
                ],
            }
        )

    return Tool(
        name="recall_memory",
        description=(
            "Full-text search across past conversation messages and saved notes. "
            "Returns the top hits ordered by relevance."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "FTS5 search query."},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
        handler=handler,
    )


# ----------------------------------------------------------------------------
def _list_conversations(db: AsyncEngine) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        channel = args.get("channel")
        since_unix = args.get("since_unix")
        limit = int(args.get("limit", 20))

        convos = await conversations.list_all(
            db,
            channel=channel if channel else None,
            since_unix=int(since_unix) if since_unix is not None else None,
            limit=limit,
        )

        out: list[dict[str, Any]] = []
        for c in convos:
            count = await conversations.message_count(db, c.id)
            out.append(
                {
                    "id": c.id,
                    "channel": c.channel,
                    "title": c.title,
                    "started_at": c.started_at,
                    "updated_at": c.updated_at,
                    "message_count": count,
                }
            )
        return json.dumps(out)

    return Tool(
        name="list_conversations",
        description="List recent conversations across channels.",
        parameters_schema={
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Optional channel filter (web | task).",
                },
                "since_unix": {
                    "type": "integer",
                    "description": (
                        "Only return conversations updated at or after this "
                        "unix timestamp."
                    ),
                },
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            },
        },
        handler=handler,
    )


# ----------------------------------------------------------------------------
def _get_conversation(db: AsyncEngine) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        conv_id = int(args.get("id", 0))
        limit = int(args.get("limit", 50))

        convo = await conversations.get(db, conv_id)
        if convo is None:
            return json.dumps({"error": f"conversation {conv_id} not found"})

        msgs = await messages.list_by_conversation(db, conv_id, limit=limit)
        return json.dumps(
            {
                "conversation": {
                    "id": convo.id,
                    "channel": convo.channel,
                    "title": convo.title,
                    "started_at": convo.started_at,
                    "updated_at": convo.updated_at,
                },
                "messages": [
                    {"id": m.id, "role": m.role, "content": m.content, "ts": m.ts}
                    for m in msgs
                ],
            }
        )

    return Tool(
        name="get_conversation",
        description="Fetch a conversation by id together with its ordered messages.",
        parameters_schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "required": ["id"],
        },
        handler=handler,
    )


# ----------------------------------------------------------------------------
def _save_note(db: AsyncEngine) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        key = str(args["key"])
        content = str(args["content"])
        tags_arg = args.get("tags")
        # Reject scalar non-string types (numbers, bools) early instead of
        # str()-coercing them into noisy tag values.
        if tags_arg is None or tags_arg == "":
            tags: str | None = None
        elif isinstance(tags_arg, str):
            tags = tags_arg.strip() or None
        elif isinstance(tags_arg, list):
            tags = ",".join(str(t) for t in tags_arg if str(t).strip()) or None
        else:
            return json.dumps({"error": "tags must be a string or array of strings"})

        note = await notes.upsert(db, key=key, content=content, tags=tags)
        return json.dumps(
            {
                "id": note.id,
                "key": note.key,
                "content": note.content,
                "tags": note.tags,
                "updated_at": note.updated_at,
            }
        )

    return Tool(
        name="save_note",
        description="Insert or update a note identified by `key`.",
        parameters_schema={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "content": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["key", "content"],
        },
        handler=handler,
    )


# ----------------------------------------------------------------------------
def _get_note(db: AsyncEngine) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        key = str(args["key"])
        note = await notes.get(db, key)
        if note is None:
            return json.dumps(None)
        return json.dumps(
            {
                "id": note.id,
                "key": note.key,
                "content": note.content,
                "tags": note.tags,
                "updated_at": note.updated_at,
            }
        )

    return Tool(
        name="get_note",
        description="Fetch a single note by its unique key.",
        parameters_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
        handler=handler,
    )


# ----------------------------------------------------------------------------
def _find_notes(db: AsyncEngine) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        query = str(args.get("query", ""))
        tags_arg = args.get("tags")
        limit = int(args.get("limit", 10))

        # Accept tags as a list (canonical), a single string, or missing.
        # Strings-of-characters-treated-as-iterable is a foot-gun; reject anything
        # else with a tool-level error so the LLM sees a clear message.
        if tags_arg is None or tags_arg == "":
            normalized_tags: list[str] = []
        elif isinstance(tags_arg, str):
            normalized_tags = [tags_arg]
        elif isinstance(tags_arg, list):
            normalized_tags = [str(t) for t in tags_arg]
        else:
            return json.dumps({"error": "tags must be a string or array of strings"})

        hits = await notes.find(db, query=query, limit=limit)

        # Whitespace-only tag entries shouldn't act as "filter out untagged
        # notes" — treat that case as no-filter.
        required = {t.strip() for t in normalized_tags if t.strip()}
        if required:
            filtered = [
                n
                for n in hits
                if n.tags and required.issubset({t.strip() for t in n.tags.split(",") if t.strip()})
            ]
        else:
            filtered = hits

        return json.dumps(
            [
                {"id": n.id, "key": n.key, "content": n.content, "tags": n.tags}
                for n in filtered
            ]
        )

    return Tool(
        name="find_notes",
        description=(
            "Full-text search across saved notes, optionally restricted to ones "
            "carrying all of the specified tags."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
        handler=handler,
    )
