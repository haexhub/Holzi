import json
import time
from typing import Any

import aiosqlite

from hermes.agent import Tool
from hermes.repository import conversations, messages
from hermes.signal.client import SignalClient

SIGNAL_CONVO_GAP_SECONDS = 6 * 3600


def build_cross_channel_tools(
    db: aiosqlite.Connection,
    signal_client: SignalClient | None,
    signal_self_number: str | None,
) -> list[Tool]:
    return [_cross_channel_send(db, signal_client, signal_self_number)]


def _cross_channel_send(
    db: aiosqlite.Connection,
    signal_client: SignalClient | None,
    self_number: str | None,
) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        channel = str(args.get("channel", ""))
        message = str(args.get("message", ""))

        if channel != "signal":
            return json.dumps(
                {"error": f"channel {channel!r} not supported (only 'signal' for now)"}
            )

        if signal_client is None or not self_number:
            return json.dumps({"error": "signal is not configured on this hermes instance"})

        now = int(time.time())
        latest = await conversations.list_by_channel(db, "signal", limit=1)
        if latest and now - latest[0].updated_at < SIGNAL_CONVO_GAP_SECONDS:
            convo = latest[0]
        else:
            convo = await conversations.create(db, channel="signal", ts=now)

        await messages.append(
            db, conversation_id=convo.id, role="assistant", content=message, ts=now
        )
        await signal_client.send(recipient=self_number, message=message)
        await conversations.touch(db, convo.id, ts=now)

        return json.dumps({"sent": True, "channel": "signal", "conversation_id": convo.id})

    return Tool(
        name="cross_channel_send",
        description=(
            "Send a message out via another channel than the one the agent is currently "
            "responding on (e.g. drop a Signal Note-to-Self from a VSCode session). "
            "Currently only `channel='signal'` is supported and the message is delivered "
            "to the linked Signal account as a Note-to-Self."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["signal"]},
                "message": {"type": "string"},
            },
            "required": ["channel", "message"],
        },
        handler=handler,
    )
