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
    "vscode": {
        "label": "VS Code Extension",
        "default_prompt": (
            "Du sprichst durch die Holzi VS Code Extension. "
            "Der User arbeitet aktiv im Editor. Antworte präzise, "
            "Code-Blöcke sind bevorzugt."
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


async def ensure_backfill(engine: AsyncEngine, *, user_id: int) -> None:
    """Seed `user_id`'s default persona + every channel row.

    Idempotent — safe to call on every boot. Two guards:
    - Personas: only inserts the default if the user has no persona. Once
      the user has any persona, don't reintroduce "Hermes" (they may have
      renamed/deleted it intentionally).
    - Channels: per-key check via `channels_repo.ensure_seeded`.

    The seed write goes through `personas_repo.create` with
    ``history_author="system"`` so the initial `persona_history`
    snapshot is tagged as system-emitted (distinct from `'user'`
    edits and `'migration'` rows).
    """
    existing_personas = await personas_repo.list_all(engine, user_id=user_id)
    if not existing_personas:
        await personas_repo.create(
            engine,
            user_id=user_id,
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


async def _resolve_persona(
    persona_id: int | None, engine: AsyncEngine, *, user_id: int
) -> Persona | None:
    """Persona by id with per-user default fallback (Plan 36 resolution order).

    The pinned `persona_id` (from a channel row) and the default fallback are
    both scoped to `user_id` — a channel that pins another user's persona, or
    a missing pin, falls through to THIS user's default persona.
    """
    persona = None
    if persona_id is not None:
        persona = await personas_repo.get(engine, persona_id, user_id=user_id)
    if persona is None:
        persona = await personas_repo.get_default(engine, user_id=user_id)
    return persona


async def _resolve_default_persona(
    channel: str, engine: AsyncEngine, *, user_id: int
) -> Persona | None:
    """Resolve the channel's default persona (row → default_persona_id → fallback)."""
    row = await channels_repo.get(engine, channel)
    persona_id = None if row is None else row.default_persona_id
    return await _resolve_persona(persona_id, engine, user_id=user_id)


async def _resolve_credential(
    engine: AsyncEngine, persona: Persona | None, *, user_id: int
) -> LlmCredential:
    """Persona credential → active credential. Raises 503 when neither exists."""
    from fastapi import HTTPException

    credential: LlmCredential | None = None
    if persona is not None and persona.llm_credential_id is not None:
        credential = await llm_credentials_repo.get(
            engine, persona.llm_credential_id, user_id=user_id
        )
    if credential is None:
        credential = await llm_credentials_repo.get_active(engine, user_id=user_id)
    if credential is None:
        raise HTTPException(
            status_code=503,
            detail=ErrorCode.PERSONA_NO_CREDENTIAL.value,
        )
    return credential


async def get_effective_system_prompt(
    channel: str,
    engine: AsyncEngine,
    *,
    user_id: int,
    persona_override: Persona | None = None,
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

    # `persona_override` supplies a one-turn persona (chat overrides); when
    # absent the channel default is resolved as documented above (scoped to
    # the calling user).
    persona = (
        persona_override
        if persona_override is not None
        else await _resolve_persona(persona_id, engine, user_id=user_id)
    )

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

    bootstrap_done = await users_mod.is_bootstrap_completed(engine, user_id)
    bootstrap_loadable = any(
        s.slug == "bootstrap-first-chat" for s in enabled_skills
    )
    if not bootstrap_done and bootstrap_loadable:
        parts.append(_BOOTSTRAP_HINT)

    return "\n\n".join(parts)


async def resolve_persona_context(
    channel: str,
    engine: AsyncEngine,
    *,
    user_id: int,
    model_override: str | None = None,
    persona_id_override: int | None = None,
) -> PersonaContext:
    """Extend get_effective_system_prompt with credential + model resolution.

    All persona lookups are scoped to `user_id` — the resolved persona is the
    calling user's, and a `persona_id_override` must belong to that user.

    Override resolution order (one-turn; not persisted):
    - persona_id_override: skips channel default persona lookup, uses this ID.
      Raises HTTPException(404, PERSONA_NOT_FOUND) if the ID doesn't exist OR
      belongs to another user.
    - model_override: supersedes persona.model / credential.model / settings.model.
    """
    from fastapi import HTTPException

    from hermes.config import settings

    # Resolve persona once (one-turn override or channel default), then reuse
    # it for the system prompt, credential and model resolution below.
    if persona_id_override is not None:
        persona = await personas_repo.get(
            engine, persona_id_override, user_id=user_id
        )
        if persona is None:
            raise HTTPException(
                status_code=404, detail=ErrorCode.PERSONA_NOT_FOUND.value
            )
    else:
        persona = await _resolve_default_persona(channel, engine, user_id=user_id)

    system_prompt = await get_effective_system_prompt(
        channel, engine, user_id=user_id, persona_override=persona
    )
    credential = await _resolve_credential(engine, persona, user_id=user_id)

    # Model resolution (override wins)
    model: str = model_override or (
        (persona.model if persona is not None else None)
        or credential.model
        or settings.model
    )

    return PersonaContext(
        system_prompt=system_prompt,
        credential=credential,
        model=model,
    )


async def resolve_chat_context_meta(
    channel: str,
    engine: AsyncEngine,
    *,
    user_id: int,
) -> tuple[int | None, str | None, str]:
    """Resolve persona_id + persona_name + model without building system_prompt.

    Used by GET /api/chat/context. Three to four DB reads — significantly
    cheaper than `resolve_persona_context` which also runs skill-catalog +
    capability-index assembly. Scoped to the calling user.
    """
    from hermes.config import settings

    persona = await _resolve_default_persona(channel, engine, user_id=user_id)
    credential = await _resolve_credential(engine, persona, user_id=user_id)

    model: str = (
        (persona.model if persona is not None else None)
        or credential.model
        or settings.model
    )

    return (
        persona.id if persona else None,
        persona.name if persona else None,
        model,
    )


async def ensure_bootstrap_skill_seeded(engine: AsyncEngine) -> None:
    """Idempotent: insert the bootstrap-first-chat skill row.

    Called once per boot (after ``ensure_platform_admin_seeded``). If the
    user has edited the body via the Skills-Page, ``ON CONFLICT (slug) DO
    NOTHING`` leaves the row untouched — no overwrite. `skills` is a
    global table (no RLS), so a direct `engine.begin()` is fine.
    """
    now = int(time.time())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO skills"
                "(slug, name, description, when_to_use, body_markdown,"
                " enabled, created_at, updated_at) "
                "VALUES (:slug, :name, :description, :when_to_use,"
                " :body_markdown, true, :now, :now) "
                "ON CONFLICT (slug) DO NOTHING"
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
