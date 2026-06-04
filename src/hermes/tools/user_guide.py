"""Tool: read a topic-specific user-guide file on demand.

The capability index in the system prompt lists which topics are
available; this tool returns the detail file for one of them. See
`hermes.capabilities` for the on-disk layout.
"""
import json
from typing import Any

from hermes import capabilities
from hermes.agent import Tool


def build_user_guide_tools() -> list[Tool]:
    return [_read_user_guide()]


def _available_topics() -> list[str]:
    return sorted(
        p.stem
        for p in capabilities.USER_GUIDE_DIR.glob("*.md")
        if p.stem != "CAPABILITIES"
    )


def _read_user_guide() -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        topic = str(args.get("topic", "")).strip().lower()
        content = capabilities.read_topic(topic)
        if content is None:
            return json.dumps(
                {
                    "error": f"unknown topic: {topic!r}",
                    "available_topics": _available_topics(),
                }
            )
        return json.dumps({"topic": topic, "content": content})

    return Tool(
        name="read_user_guide",
        description=(
            "Read a detailed user-guide article about a Holzi feature. "
            "Available topic slugs are listed in the capability index in "
            "the system prompt. Call this when the user asks how to use "
            "a specific feature in depth."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        "Slug of the topic (e.g. 'workspaces', 'memory', "
                        "'tasks'). See the capability index for the full "
                        "list."
                    ),
                }
            },
            "required": ["topic"],
        },
        handler=handler,
    )
