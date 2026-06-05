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
import json
import time
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes import capabilities
from hermes import users as users_mod
from hermes.errors import ErrorCode
from hermes.repository import channels as channels_repo
from hermes.repository import llm_credentials as llm_credentials_repo
from hermes.repository import personas as personas_repo
from hermes.repository import skills as skills_repo
from hermes.repository.models import LlmCredential, Persona, Skill

@dataclass
class PersonaContext:
    """Resolved agent context for a single chat turn.

    `credential` is always non-null: resolved from persona.llm_credential_id
    → active credential → HTTPException(503) if neither exists.
    `model` is persona.model if set, else credential.model, else settings.model.
    """
    system_prompt: str
    credential: LlmCredential
    model: str


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

# Plan 36: persona prompt is three typed fragments. `ensure_backfill`
# seeds these directly; the legacy `DEFAULT_PERSONA_PROMPT` constant was
# removed in Task 5.
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

# ---------------------------------------------------------------------------
# Bootstrap-skill seed constants (Plan 37 Task 6)
# ---------------------------------------------------------------------------

BOOTSTRAP_SKILL_DESCRIPTION: Final[str] = (
    "Onboarding-Q&A für eine frische Holzi-Installation. Stellt 3-5 "
    "Fragen und schreibt das Ergebnis in die Default-Persona."
)

BOOTSTRAP_SKILL_WHEN_TO_USE: Final[str] = (
    "Erste User-Message in einer frischen Installation, sobald der "
    "bootstrap-Hint im System-Prompt erscheint. Auch manuell durch "
    "den User: \"setze mich neu auf\"."
)

BOOTSTRAP_SKILL_BODY: Final[str] = """\
# bootstrap-first-chat

> Du bist gerade dabei, Holzi für einen neuen User aufzusetzen.
> Wenn der User auf Englisch antwortet, wechsle zu Englisch und
> übersetze die folgenden Fragen sinngemäß.

Stelle dem User die folgenden Fragen — **eine nach der anderen**.
Warte auf jede Antwort, bevor du die nächste stellst.

### Frage 1 — Identität

„Hallo! Ich bin Hermes, dein persönlicher KI-Assistent. Wer bist
du? Erzähl mir kurz deinen Namen und was du beruflich (oder als
Hauptbeschäftigung) machst."

### Frage 2 — Stil

„Wie soll ich mit dir reden? Eher direkt-sachlich (Senior-Engineer-
Modus, keine Floskeln), eher ausführlich-erklärend (Teaching-Modus),
oder ausgeglichen?"

### Frage 3 — Hauptanwendungsfälle

„Wofür willst du mich vor allem benutzen? (z.B. Coding, Recherche,
Schreiben, Lernen, Reflektion, Familie / Alltag, …)"

### Optional Frage 4 — Lieblings-Tools

„Gibt es bestimmte Tools oder Themen, die du oft benutzen wirst und
die ich kennen sollte? (Optional — du kannst auch „skip" sagen.)"

### Abschluss

Wenn du genug hast, mach Folgendes — **in dieser Reihenfolge**:

1. Rufe `persona_update(soul=..., identity=..., agents=...)` mit
   den drei Fragments synthetisiert aus den User-Antworten:
   - `identity` ≈ Name + Rolle (Antwort 1)
   - `soul` ≈ Ton-Präferenz (Antwort 2)
   - `agents` ≈ Anwendungsfälle als „Du fokussierst auf …"-Liste
     (Antwort 3)
2. Optional: Falls Frage 4 spezifische Tools oder Themen lieferte,
   rufe für jedes 1× `save_note(key=..., content=..., tags=...)`.
3. Rufe `mark_bootstrap_complete()`.
4. Antworte dem User mit einer kurzen Zusammenfassung dessen, was
   du gesetzt hast, und einem Hinweis auf `/settings/preferences`,
   wo der User die Werte editieren kann.

### Wenn der User nicht mitspielt

Drei Fälle, jeweils mit klarer Anweisung an dich (den Agenten):

**1. User antwortet off-topic** (z.B. „erzähl mir einen Witz",
„erkläre Quantenphysik"):
Antworte kurz: „Lass mich Holzi erst für dich aufsetzen, dann
können wir frei chatten. Zurück zu Frage X: …" und stelle die
laufende Frage erneut. Maximal ein Mal pro Frage — wenn der User
beim zweiten Versuch immer noch ausweicht, behandle das als
implizites Skip (siehe Fall 2).

**2. User sagt explizit Skip** („skip", „überspringen", „abbrechen",
„nicht jetzt", oder vergleichbar):
- Rufe **nur** `mark_bootstrap_complete()` — kein
  `persona_update`-Call.
- Antworte: „Ok, ich überspringe das Setup. Du kannst es jederzeit
  unter /settings/preferences nachholen."

**3. Nach 10 ausgetauschten Nachrichten (5 Fragen + 5 Antworten)
ist immer noch keine Persona gesetzt:**
Brich die Q&A ab. Rufe `mark_bootstrap_complete()`. Wenn du
trotzdem genug Information hast, kannst du vorher ein
`persona_update(...)` mit dem was du hast machen — sonst nur das
`mark`. Antworte freundlich: „Wir können das später fortsetzen
unter /settings/preferences."

Diese drei Regeln sind reiner Body-Text in diesem Skill. Es gibt
**keinen Server-Side-Mechanismus**, der nach 10 Turns automatisch
das Bootstrap-Flag flippt — die Verantwortung liegt vollständig bei
dir als Agent. Wenn du den Skill abbrichst ohne
`mark_bootstrap_complete()` aufzurufen, wird die nächste frische
Conversation den Bootstrap-Hint erneut sehen und du wirst nochmal
versuchen müssen, den User durch die Q&A zu führen. Das ist
beabsichtigt — kein silent fallback.
"""


async def _migrate_prompt_to_fragments(engine: AsyncEngine) -> None:
    """One-shot lifespan migration: bring `personas` up to the Plan-36
    shape (soul/identity/agents) from the pre-Plan-36 single-`prompt`
    shape.

    Plan-36 changes the `personas` table from a single `prompt` text
    column to three typed fragments (`soul`, `identity`, `agents`).
    SQLAlchemy's `metadata.create_all` issues `CREATE TABLE IF NOT
    EXISTS` and does NOT alter existing tables, so on a legacy DB the
    three new columns are missing — this helper adds them, copies the
    old `prompt` into `identity`, writes a baseline `persona_history`
    row per migrated persona (so the audit trail is complete from
    day-one), and finally drops the `prompt` column.

    Idempotent: a single `PRAGMA table_info(personas)` check on the
    `prompt` column drives the whole branch — present means "legacy
    DB, run migration"; absent means "already on Plan-36 shape, no-op".
    Safe to delete once every deployed box has booted on Plan-36 code
    (tracked as a follow-up; see Plan 36 Risk Register).

    The `WHERE identity = ''` guard on the UPDATE prevents clobbering
    any `identity` content a user may have written between two boot
    cycles in the theoretical "partial migration → crash → restart"
    scenario.
    """
    async with engine.connect() as conn:
        cols = (await conn.execute(text("PRAGMA table_info(personas)"))).all()
        has_prompt = any(row.name == "prompt" for row in cols)
    if not has_prompt:
        return

    async with engine.begin() as conn:
        # ALTER TABLE ADD COLUMN is idempotent only through the PRAGMA
        # guard above; SQLite has no "ADD COLUMN IF NOT EXISTS".
        for col in ("soul", "identity", "agents"):
            await conn.execute(
                text(
                    f"ALTER TABLE personas ADD COLUMN {col} "
                    "TEXT NOT NULL DEFAULT ''"
                )
            )
        await conn.execute(
            text("UPDATE personas SET identity = prompt WHERE identity = ''")
        )
        # Baseline history row per migrated persona so the Verlauf-Tab
        # shows the migrated-from state. author='migration' distinguishes
        # this from user-edit ('user') and seed ('system') rows. The
        # snapshot reflects the POST-migration values (i.e. soul='',
        # identity=<legacy prompt>, agents='').
        now = int(time.time())
        rows = (
            await conn.execute(
                text("SELECT id, name, soul, identity, agents FROM personas")
            )
        ).all()
        for row in rows:
            snapshot = json.dumps(
                {
                    "name": row.name,
                    "soul": row.soul,
                    "identity": row.identity,
                    "agents": row.agents,
                }
            )
            await conn.execute(
                text(
                    "INSERT INTO persona_history "
                    "(persona_id, author, snapshot_json, created_at) "
                    "VALUES (:pid, 'migration', :snap, :ts)"
                ),
                {"pid": row.id, "snap": snapshot, "ts": now},
            )
        await conn.execute(text("ALTER TABLE personas DROP COLUMN prompt"))


async def _drop_persona_skills_table(engine: AsyncEngine) -> None:
    """One-shot: drop the Plan-33 `persona_skills` table if it still
    exists. Idempotent.
    """
    async with engine.connect() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='persona_skills'"
                )
            )
        ).all()
    if not tables:
        return
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE persona_skills"))


async def _migrate_skills_add_enabled(engine: AsyncEngine) -> None:
    """One-shot: add `skills.enabled` if missing. Idempotent —
    PRAGMA-gated. Existing rows default to 1 (enabled).
    """
    async with engine.connect() as conn:
        cols = (
            await conn.execute(text("PRAGMA table_info(skills)"))
        ).all()
        has_enabled = any(row.name == "enabled" for row in cols)
    if has_enabled:
        return
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "ALTER TABLE skills ADD COLUMN enabled INTEGER "
                "NOT NULL DEFAULT 1"
            )
        )


async def _migrate_personas_add_credential_columns(engine: AsyncEngine) -> None:
    """One-shot: add llm_credential_id + model to personas if missing. Idempotent."""
    async with engine.connect() as conn:
        cols = (await conn.execute(text("PRAGMA table_info(personas)"))).all()
        has_cred = any(row.name == "llm_credential_id" for row in cols)
    if has_cred:
        return
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "ALTER TABLE personas ADD COLUMN llm_credential_id INTEGER "
                "REFERENCES llm_credentials(id) ON DELETE SET NULL"
            )
        )
        await conn.execute(
            text("ALTER TABLE personas ADD COLUMN model TEXT")
        )


async def ensure_backfill(engine: AsyncEngine) -> None:
    """Seed the default persona + every channel row that's missing.

    Idempotent — safe to call on every boot. Two guards:
    - Personas: only inserts the default if the table is empty. Once the
      user has any persona, don't reintroduce "Hermes" (they may have
      renamed/deleted it intentionally).
    - Channels: per-key check via `channels_repo.ensure_seeded`.

    The seed write goes through `personas_repo.create` with
    ``history_author="system"`` so the initial `persona_history`
    snapshot is tagged as system-emitted (distinct from `'user'`
    edits and `'migration'` rows produced by
    `_migrate_prompt_to_fragments`).
    """
    existing_personas = await personas_repo.list_all(engine)
    if not existing_personas:
        await personas_repo.create(
            engine,
            name=DEFAULT_PERSONA_NAME,
            soul=DEFAULT_PERSONA_SOUL,
            identity=DEFAULT_PERSONA_IDENTITY,
            agents=DEFAULT_PERSONA_AGENTS,
            is_default=True,
            history_author="system",
        )
    await channels_repo.ensure_seeded(engine)


def _persona_sections(persona: Persona) -> list[tuple[str, str]]:
    """Hardcoded section order Soul → Identity → Agents.

    The resolver filters out tuples whose body is empty after `.strip()`
    so the section header isn't rendered for an empty fragment. Order is
    deliberately not configurable — every persona renders sections in
    the same sequence so prompt-engineering effects are stable.
    """
    return [
        ("## Soul", persona.soul),
        ("## Identity", persona.identity),
        ("## Agents", persona.agents),
    ]


def _catalog_index(skills: list[Skill]) -> str:
    """Render the ``## Available skills`` catalog section.

    One line per enabled skill: ``- {slug} — {description} (use when: {when_to_use})``.
    The ``(use when: ...)`` suffix is omitted when ``when_to_use`` is empty.
    Skills are already sorted alphabetically by slug (caller passes
    ``skills_repo.list_enabled()`` output). Returns empty string when
    the list is empty — the resolver then skips this section entirely.
    """
    if not skills:
        return ""
    lines: list[str] = ["## Available skills"]
    for skill in skills:
        if skill.when_to_use:
            lines.append(
                f"- {skill.slug} — {skill.description} (use when: {skill.when_to_use})"
            )
        else:
            lines.append(f"- {skill.slug} — {skill.description}")
    return "\n".join(lines)


_BOOTSTRAP_HINT = (
    "---\n\n"
    "You haven't been set up yet. As your first action, call "
    "skill_load('bootstrap-first-chat') and follow its instructions "
    "before responding to the user."
)


async def get_effective_system_prompt(
    channel: str, engine: AsyncEngine
) -> str:
    """Compose the system prompt the agent should run with for `channel`.

    Composition order (Plan 37 extends 36 with catalog index + bootstrap
    hint):

    ```
    ## Soul
    <persona.soul>

    ## Identity
    <persona.identity>

    ## Agents
    <persona.agents>

    ## Available skills
    - {slug} — {description} (use when: {when_to_use})
    ...

    <capability_index>

    <channel.prompt>

    <bootstrap_hint>   # only when bootstrap_completed = 0
    ```

    Persona-section rules:
    - Each section is rendered as ``"## Header\\n<body>"``.
    - Sections with empty (post-`.strip()`) body are omitted entirely —
      no leading header without body, no double separator.
    - Order is fixed Soul → Identity → Agents regardless of how the
      `Persona` dataclass was constructed.

    Catalog-index rules (Plan 37):
    - One line per enabled skill, alphabetical by slug.
    - ``(use when: ...)`` suffix omitted when ``when_to_use`` is empty.
    - Section omitted entirely when zero enabled skills exist.

    Bootstrap-hint (Plan 37):
    - Appended after ``channel_prompt`` when ``users.bootstrap_completed = 0``
      AND the ``bootstrap-first-chat`` skill is present + enabled.
    - Omitted when no ``users`` row exists (defensive), after
      ``mark_bootstrap_complete()`` flips the flag, or when the
      bootstrap skill was disabled/deleted — otherwise the agent would
      follow the hint and hit a 404 on ``skill_load``.

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

    enabled_skills = await skills_repo.list_enabled(engine)
    catalog = _catalog_index(enabled_skills)

    index = capabilities.load_capability_index()
    parts: list[str] = []

    if persona is not None:
        persona_parts: list[str] = []
        for header, body in _persona_sections(persona):
            body = body.strip()
            if body:
                persona_parts.append(f"{header}\n{body}")
        if persona_parts:
            parts.append("\n\n".join(persona_parts))

    if catalog:
        parts.append(catalog)
    if index:
        parts.append(index)
    parts.append(channel_prompt)

    bootstrap_done = await users_mod.is_bootstrap_completed(engine)
    bootstrap_loadable = any(
        s.slug == "bootstrap-first-chat" for s in enabled_skills
    )
    if not bootstrap_done and bootstrap_loadable:
        parts.append(_BOOTSTRAP_HINT)

    return "\n\n".join(parts)


async def resolve_persona_context(
    channel: str,
    engine: AsyncEngine,
) -> PersonaContext:
    """Extend get_effective_system_prompt with credential + model resolution.

    Resolution order:
    1. system_prompt: delegates to get_effective_system_prompt.
    2. credential: persona.llm_credential_id → active credential.
       Raises HTTPException(503, PERSONA_NO_CREDENTIAL) when neither found.
    3. model: persona.model → credential.model → settings.model.
    """
    from fastapi import HTTPException
    from hermes.config import settings

    system_prompt = await get_effective_system_prompt(channel, engine)

    row = await channels_repo.get(engine, channel)
    persona_id: int | None = None if row is None else row.default_persona_id
    persona = None
    if persona_id is not None:
        persona = await personas_repo.get(engine, persona_id)
    if persona is None:
        persona = await personas_repo.get_default(engine)

    credential: LlmCredential | None = None
    if persona is not None and persona.llm_credential_id is not None:
        credential = await llm_credentials_repo.get(engine, persona.llm_credential_id)
    if credential is None:
        credential = await llm_credentials_repo.get_active(engine)
    if credential is None:
        raise HTTPException(
            status_code=503,
            detail=ErrorCode.PERSONA_NO_CREDENTIAL.value,
        )

    model: str = (
        (persona.model if persona is not None else None)
        or credential.model
        or settings.model
    )

    return PersonaContext(
        system_prompt=system_prompt,
        credential=credential,
        model=model,
    )


async def ensure_bootstrap_skill_seeded(engine: AsyncEngine) -> None:
    """Idempotent: INSERT OR IGNORE the bootstrap-first-chat skill row.

    Called once per boot (after ``ensure_users_seeded``). If the user
    has edited the body via the Skills-Page, ``INSERT OR IGNORE`` matched
    on the UNIQUE slug leaves the row untouched — no overwrite.
    """
    now = int(time.time())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO skills"
                "(slug, name, description, when_to_use, body_markdown,"
                " enabled, created_at, updated_at) "
                "VALUES (:slug, :name, :description, :when_to_use,"
                " :body_markdown, 1, :now, :now)"
            ),
            {
                "slug": "bootstrap-first-chat",
                "name": "Bootstrap: First Chat",
                "description": BOOTSTRAP_SKILL_DESCRIPTION,
                "when_to_use": BOOTSTRAP_SKILL_WHEN_TO_USE,
                "body_markdown": BOOTSTRAP_SKILL_BODY,
                "now": now,
            },
        )
