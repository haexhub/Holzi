"""Personas + channel-prompt resolver (Plan 29-A).

This module is the single source of truth for two intertwined concepts:

- **Personas** (DB rows in `personas`) — the *who* of the agent: name +
  prompt + an `is_default` flag. CRUD lives in
  `hermes.repository.personas`.
- **Channel prompts** (DB rows in `channel_prompts`) — the *how* of each
  channel: format/length/tone overlay for `web` and `task` today. CRUD
  lives in `hermes.repository.channels`.

`CHANNEL_REGISTRY` enumerates the channels the codebase knows about and
provides the default prompt text used when a channel row is first
seeded. Adding a new channel (e.g. `discord`) is a one-line registry
edit plus a call-site update — the schema and UI render automatically
because both iterate the registry.

The resolver `get_effective_system_prompt(channel, db)` composes the
chosen persona's prompt with the chosen channel's prompt at runtime —
the four call-sites that used to inline `*_SYSTEM_PROMPT` constants now
go through this single function.
"""
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes import capabilities
from hermes.repository import channels as channels_repo
from hermes.repository import personas as personas_repo
from hermes.repository import skills as skills_repo

# Single source of truth for known channels. Key = exact string used as
# the channel identifier by the call-sites and persisted in the
# `channel_prompts.channel` column. To add a new channel, append an
# entry here; `ensure_backfill` will seed the row on next boot, the
# resolver will compose for it, and the /api/channels endpoint will
# render it. Default prompts are in German (UI is German-first; Plan 30
# adds i18n).
CHANNEL_REGISTRY: Final[dict[str, dict[str, str]]] = {
    "web": {
        "label": "Web-Chat",
        "default_prompt": (
            "Du sprichst durch das Web-UI. Markdown ist OK, "
            "längere Antworten sind OK."
        ),
    },
    "task": {
        "label": "Geplante Tasks",
        "default_prompt": (
            "Du führst einen geplanten Task autonom aus. Es gibt keinen "
            "User am anderen Ende. Antworte knapp und fokussiert auf das "
            "Resultat."
        ),
    },
}

# Seeded into `personas` on first boot. Re-creates carry forward whatever
# the user has edited — backfill is gated on "table empty" rather than
# "row with this name absent".
DEFAULT_PERSONA_NAME: Final[str] = "Hermes"
DEFAULT_PERSONA_PROMPT: Final[str] = (
    "Du bist Hermes, ein persönlicher KI-Assistent für Martin. "
    "Sei direkt, präzise und technisch."
)

# Plan 36: persona prompt split into three typed fragments. The default
# constants exist alongside DEFAULT_PERSONA_PROMPT until Task 5 reshapes
# `ensure_backfill` to seed the new columns.
DEFAULT_PERSONA_SOUL: Final[str] = (
    "Du bist direkt, präzise und technisch. Keine Floskeln, keine "
    "Höflichkeitswulst — der User ist Senior-Engineer."
)
DEFAULT_PERSONA_IDENTITY: Final[str] = (
    "Du bist Hermes, ein persönlicher KI-Assistent."
)
DEFAULT_PERSONA_AGENTS: Final[str] = (
    "Du befolgst Test-Driven-Development: erst die Tests, dann die "
    "Implementierung. Du fragst nach, bevor du destruktive Aktionen "
    "ausführst."
)


async def _migrate_prompt_to_fragments(engine: AsyncEngine) -> None:
    """One-shot Plan 36 migration: copy `personas.prompt` into `identity`
    and drop the old column.

    Idempotent — on a fresh DB the column never existed, on an
    already-migrated DB the PRAGMA check returns no `prompt` row, and
    this is a no-op. The new columns (`soul`/`identity`/`agents`) are
    expected to already exist on the table at call time;
    `metadata.create_all` adds them on every boot via `schema.py`. Safe
    to delete after every deployed box has booted once on Plan-36 code.
    """
    async with engine.connect() as conn:
        cols = (await conn.execute(text("PRAGMA table_info(personas)"))).all()
        has_prompt = any(row.name == "prompt" for row in cols)
    if not has_prompt:
        return
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE personas SET identity = prompt WHERE identity = ''")
        )
        await conn.execute(text("ALTER TABLE personas DROP COLUMN prompt"))


async def ensure_backfill(engine: AsyncEngine) -> None:
    """Seed the default persona + every channel row that's missing.

    Idempotent — safe to call on every boot. Two guards:
    - Personas: only inserts the default if the table is empty. Once the
      user has any persona, don't reintroduce "Hermes" (they may have
      renamed/deleted it intentionally).
    - Channels: per-key check via `channels_repo.ensure_seeded`.
    """
    existing_personas = await personas_repo.list_all(engine)
    if not existing_personas:
        await personas_repo.create(
            engine,
            name=DEFAULT_PERSONA_NAME,
            prompt=DEFAULT_PERSONA_PROMPT,
            is_default=True,
        )
    await channels_repo.ensure_seeded(engine)


async def get_effective_system_prompt(
    channel: str, engine: AsyncEngine
) -> str:
    """Compose the system prompt the agent should run with for `channel`.

    Composition order (Plan 33 extends 29-A by inserting skills between
    persona and capability_index):

    `persona.prompt + skills_block + capability_index + channel.prompt`

    `skills_block` is the join of every active (enabled=1) skill body
    attached to the resolved persona, in `ordering` order. Any of the
    first three components may be empty/missing and is then skipped —
    the separator stays consistent between the parts that actually
    appear.

    Persona resolution:
    1. `channel_prompts.default_persona_id` if set and the row exists.
    2. Otherwise the globally-default persona (`personas.is_default = 1`).
    3. If neither exists (theoretically impossible after backfill, but
       robust against a corrupted DB), the channel prompt is returned
       with the index alone (or by itself if there is no index).

    Raises `KeyError` for channel keys not in `CHANNEL_REGISTRY` — the
    call-site is buggy if it passes one. Channel rows are guaranteed to
    exist after `ensure_backfill`; the defensive fallback below catches
    a manually-truncated `channel_prompts` table.
    """
    if channel not in CHANNEL_REGISTRY:
        raise KeyError(f"unknown channel: {channel}")

    row = await channels_repo.get(engine, channel)
    if row is None:
        channel_prompt = CHANNEL_REGISTRY[channel]["default_prompt"]
        persona_id: int | None = None
    else:
        channel_prompt = row.prompt
        persona_id = row.default_persona_id

    persona = None
    if persona_id is not None:
        persona = await personas_repo.get(engine, persona_id)
    if persona is None:
        persona = await personas_repo.get_default(engine)

    skills_block = ""
    if persona is not None:
        attached = await skills_repo.list_for_persona(engine, persona.id)
        active_bodies = [
            skill.body_markdown
            for skill, _ordering, enabled in attached
            if enabled and skill.body_markdown.strip()
        ]
        skills_block = "\n\n".join(active_bodies)

    index = capabilities.load_capability_index()
    parts: list[str] = []
    if persona is not None and persona.prompt.strip():
        parts.append(persona.prompt)
    if skills_block:
        parts.append(skills_block)
    if index:
        parts.append(index)
    parts.append(channel_prompt)
    return "\n\n".join(parts)
