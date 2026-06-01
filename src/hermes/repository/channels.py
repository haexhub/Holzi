"""Persistence layer for `channel_prompts` (Plan 29-A).

One row per channel key registered in `hermes.personas.CHANNEL_REGISTRY`
(`web`, `task`, `signal`, `telegram`). `ensure_seeded` is idempotent and
is called once from the lifespan: each missing channel gets a row with
its `default_prompt` and `default_persona_id = NULL` (the resolver then
falls back to the globally-default persona).

`update` / `reset_prompt` return None for channel keys not in the
registry — the route layer surfaces those as 404. Wrong persona ids in
`update` aren't caught here; the route layer validates them before
calling (412/422 fits the contract better than the raw IntegrityError).
"""
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository.models import ChannelPromptRow
from hermes.schema import channel_prompts as t_channels


def _row_to_channel(row) -> ChannelPromptRow:
    return ChannelPromptRow(
        channel=row.channel,
        prompt=row.prompt,
        default_persona_id=row.default_persona_id,
        updated_at=row.updated_at,
    )


async def list_all(engine: AsyncEngine) -> list[ChannelPromptRow]:
    """Return rows in `CHANNEL_REGISTRY` order so the UI cards always
    render in the same canonical sequence."""
    # Import locally so the schema bootstrap path can import this module
    # before personas-the-module finishes loading.
    from hermes.personas import CHANNEL_REGISTRY

    async with engine.connect() as conn:
        result = await conn.execute(select(t_channels))
        rows = result.all()
    by_key = {r.channel: r for r in rows}
    return [
        _row_to_channel(by_key[k])
        for k in CHANNEL_REGISTRY
        if k in by_key
    ]


async def get(
    engine: AsyncEngine, channel: str
) -> ChannelPromptRow | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_channels).where(t_channels.c.channel == channel)
        )
        row = result.first()
    return _row_to_channel(row) if row is not None else None


async def ensure_seeded(
    engine: AsyncEngine, *, ts: int | None = None
) -> list[str]:
    """Insert a row for every key in `CHANNEL_REGISTRY` that isn't already
    in `channel_prompts`. Returns the list of newly-inserted channel keys
    (so the lifespan can log a count). Idempotent: a repeat call returns
    `[]` once every channel exists."""
    from hermes.personas import CHANNEL_REGISTRY

    now = ts if ts is not None else int(time.time())
    inserted: list[str] = []
    async with engine.begin() as conn:
        result = await conn.execute(select(t_channels.c.channel))
        existing = {row.channel for row in result.all()}
        for channel, registry_entry in CHANNEL_REGISTRY.items():
            if channel in existing:
                continue
            await conn.execute(
                t_channels.insert().values(
                    channel=channel,
                    prompt=registry_entry["default_prompt"],
                    default_persona_id=None,
                    updated_at=now,
                )
            )
            inserted.append(channel)
    return inserted


# Sentinel for `update(default_persona_id=...)` to distinguish "don't
# touch" (None argument default) from "set to NULL" (caller passes None
# explicitly). Python kwargs collapse both into None otherwise.
_UNSET: object = object()


async def update(
    engine: AsyncEngine,
    channel: str,
    *,
    prompt: str | None = None,
    default_persona_id: int | None | object = _UNSET,
    ts: int | None = None,
) -> ChannelPromptRow | None:
    """Patch a channel row. None for `prompt` means "don't touch". For
    `default_persona_id`, the sentinel distinguishes "don't touch"
    (omitted) from "set to NULL" (explicit None). Returns None if the
    channel key is unknown."""
    from hermes.personas import CHANNEL_REGISTRY

    if channel not in CHANNEL_REGISTRY:
        return None
    existing = await get(engine, channel)
    if existing is None:
        return None

    new_prompt = prompt if prompt is not None else existing.prompt
    if default_persona_id is _UNSET:
        new_persona_id: int | None = existing.default_persona_id
    else:
        # The sentinel branch is exhausted; the remaining type is int|None.
        assert default_persona_id is None or isinstance(default_persona_id, int)
        new_persona_id = default_persona_id

    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        await conn.execute(
            t_channels.update()
            .where(t_channels.c.channel == channel)
            .values(
                prompt=new_prompt,
                default_persona_id=new_persona_id,
                updated_at=now,
            )
        )
    return await get(engine, channel)


async def reset_prompt(
    engine: AsyncEngine, channel: str, *, ts: int | None = None
) -> ChannelPromptRow | None:
    """Set `prompt` back to `CHANNEL_REGISTRY[channel]['default_prompt']`.
    Leaves `default_persona_id` untouched. Returns None for unknown channel."""
    from hermes.personas import CHANNEL_REGISTRY

    if channel not in CHANNEL_REGISTRY:
        return None
    return await update(
        engine,
        channel,
        prompt=CHANNEL_REGISTRY[channel]["default_prompt"],
        ts=ts,
    )
