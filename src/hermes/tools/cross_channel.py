import json
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from hermes.agent import Tool
from hermes.repository import conversations, messages
from hermes.signal.client import SignalClient

SIGNAL_CONVO_GAP_SECONDS = 6 * 3600


def build_cross_channel_tools(
    db: AsyncConnection,
    signal_client: SignalClient | None,
    signal_self_number: str | None,
    *,
    current_channel: str | None = None,
) -> list[Tool]:
    """Build the cross-channel tool catalog.

    `current_channel`, if set, identifies the channel the agent is currently
    responding on. cross_channel_send refuses to write back to that channel
    so a Web-UI run can't trigger another Web-UI run by sending to itself
    (and likewise for Signal once the Signal worker starts using the
    catalog).
    """
    return [_cross_channel_send(db, signal_client, signal_self_number, current_channel)]


def _cross_channel_send(
    db: AsyncConnection,
    signal_client: SignalClient | None,
    self_number: str | None,
    current_channel: str | None,
) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        channel = str(args.get("channel", ""))
        message = str(args.get("message", ""))

        if current_channel is not None and channel == current_channel:
            return json.dumps(
                {
                    "error": (
                        f"cross_channel_send cannot write back to the current "
                        f"channel {current_channel!r}"
                    )
                }
            )

        if channel != "signal":
            return json.dumps(
                {"error": f"channel {channel!r} not supported (only 'signal' for now)"}
            )

        if signal_client is None or not self_number:
            return json.dumps({"error": "signal is not configured on this hermes instance"})

        # Send first — if signal-cli rejects the call, leave the DB untouched
        # so the conversation log never claims a delivery that didn't happen.
        await signal_client.send(recipient=self_number, message=message)

        now = int(time.time())
        latest = await conversations.list_by_channel(db, "signal", limit=1)
        if latest and now - latest[0].updated_at < SIGNAL_CONVO_GAP_SECONDS:
            convo = latest[0]
        else:
            convo = await conversations.create(db, channel="signal", ts=now)

        await messages.append(
            db, conversation_id=convo.id, role="assistant", content=message, ts=now
        )
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
