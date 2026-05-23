# Next session — 2026-05-25

Letzte Session (2026-05-24) hat den LLM-Credentials-Stack durch eine umfangreiche OAuth-Sackgasse geführt, am Ende aber sauber abgeschlossen. Chat funktioniert lokal end-to-end via Claude Max OAuth. Lies zuerst die "Was gerade lief" Sektion, dann "Pre-flight" — danach pick ein Item aus der Roadmap.

## Was gerade lief

Session 2026-05-24, ehrlich aufgearbeitet:

| PR | Repo | Title | State |
|---|---|---|---|
| #19 | Holzi | per-credential model + GET /credentials/{id}/models | merged |
| #20 | Holzi | /api/chat error semantics 502/504 (Roadmap C.5) | merged |
| #21 | Holzi | activate-guard für pending OAuth → 409 | merged |
| #22 | Holzi | setup-token PTY driver — **war kaputt, nicht nutzen** | merged 😬 |
| #23 | Holzi | revert #22 + claude 2.1.126 pin + upstream Anthropic-via-proxy | **OFFEN** |
| #8 | holzi-frontend | ModelSelect Combobox | merged |
| #9 | holzi-frontend | Aktivieren-Button hide für unready OAuth | merged |

**Was war der OAuth-Detour?** claude-code 2.1.121 hat den `Paste code here if prompted >` Prompt aus `auth login --claudeai` stillschweigend entfernt. Statt einfach claude-code zu bumpen hab ich versucht stattdessen `setup-token` zu nutzen, was Bracketed-Paste + PTY + Token-Extraction nötig macht, plus die Tokens werden direkt gegen `api.anthropic.com` blockt (429 "Error"). #22 hat das gemerged, aber war broken. #23 revertiert alles + bumpt claude auf 2.1.126 (Specifyrs Version, hat den Prompt noch). Details siehe Memory `project-hermes-oauth-claude-2-1`.

## Pre-flight

1. **#23 mergen** — saubere Fix-PR mit allen Reverts + claude 2.1.126. CI grün, CodeRabbit triagen + merge. Ohne diesen Merge ist `main` auf github broken (nicht aber das lokale Image, das läuft mit den Working-Tree-Stand vor dem Pull).
2. Sync `main` lokal + image rebuild: `git pull && make up-local-full`. Local `oauth.py` schon korrekt (working tree).

## Roadmap (offen, prio-sortiert)

### A. Frontend polish (alles unabhängig, pick eins oder mehrere)

| Item | Größe | Notes |
|---|---|---|
| **Conversation auto-title aus erster Message** | mittel | UI zeigt "web #N". Server-side derive: `routes/api.py:api_chat` macht `if payload.conversation_id is None: title = payload.message[:40]` und passt `conversations.create()` an. Plus `PATCH /conversations/{id}` Endpoint für Rename. |
| **Mobile Layout (3-Spalten-Collapse)** | mittel | Fixed `grid-cols-[260px_1fr_320px]` in `app/pages/index.vue` (oder layout). Auf `<md` → Drawer für Sidebar + Right-Panel als Tabs. Tailwind + headless-ui. |
| **SSE Reconnect on drop** | mittel | `useChatStream.ts` braucht retry-with-backoff. EventSource gibt `onerror` event; auto-reopen mit Backoff (1s, 2s, 4s, …, max 30s). State-machine: `'streaming' | 'reconnecting' | 'failed'`. |
| **Dark-mode Toggle** | klein | CSS-Vars schon da in `tailwind.css`. `@vueuse/core`'s `useColorMode`. Toggle-Button im Header. |
| **Empty-state für First-Time-User** | klein | Wenn `credentials.length == 0` auf `/settings/llm` UND `conversations.length == 0` auf `/` → Empty-State mit "Start by adding credentials" Link. |
| **Token-by-Token typing animation** | klein | Backend streamt schon `text` events. `useChatStream`'s onText callback ist da, aber das Chat-View renders Vollständig — auf incremental rendering umstellen. |
| **Bessere upstream-Error Toasts** | klein | #20 hat `error` SSE event mit `{code, status_code, message}`. Frontend liest aktuell nur `message`. Branching auf `code` → freundliche Texte ("Provider nicht erreichbar" / "Provider zu langsam" / "Interner Fehler"). |

### B. Backend polish

| Item | Größe | Notes |
|---|---|---|
| **E2E `/api/chat` → agent → tool roundtrip test** | mittel | pytest-fixture mit mock upstream, assert auf message-history mit user→assistant(tool_call)→tool→assistant turns. |
| **Coverage Reporting in CI** | klein | `pytest --cov` + Codecov action. Vitest `--coverage` für frontend. |
| **Connection-pool sanity check** | klein | Default `AsyncAdaptedQueuePool` size = 5. Synthetic-load test (mehrere parallele /api/chat) + check für QueuePool-warnings. |
| **`conn` → `engine` rename** | klein | Test-fixture name. Single sed-sweep auf ~15 test-files. |
| **`aiosqlite` aus pyproject droppen** | klein | Transitive via `sqlalchemy[asyncio]`, top-level entry redundant. |
| **mypy --strict feasibility check** | klein | Aktuell default config. `mypy --strict src` ausführen, sehen wieviele errors, evtl. enablen. |

### C. Post-MVP (nach Roadmap E)

| Item | Größe | Notes |
|---|---|---|
| **External MCP client manager** | groß | `HERMES_EXTERNAL_MCP` env (JSON `[{name, url, token?}]`), manifest-fetch beim boot, tools gemerged in agent-catalogue mit `<name>.<tool>` namespace. Erstes Target: haex-vault. |
| **haex-vault MCP integration** | mittel | Nach client-manager: `vault.list_spaces`, `vault.create_sync_rule`, `vault.read_file` etc. exposed über external-MCP. |
| **File uploads + IndexedDB staging** | mittel | Frontend staged in IndexedDB bevor backend hits — schont mobile networks bei Drafts. |
| **At-rest encryption auf hermes.db** | mittel | SQLCipher integration. Aktuell relied auf VPS-disk-encryption + TLS. |
| **Token rotation für HERMES_AUTH_TOKEN** | mittel | Aktuell ein fixed secret. Rotation-Schema mit overlap-window. |

### D. Production deploy (Roadmap A)

| Item | Größe | Notes |
|---|---|---|
| **Frontend → backend hand-off Dockerfile** | mittel | Multi-stage in Holzi pulled+built `holzi-frontend/main`. Single `docker compose up` deploy. |
| **Compose `--profile traefik` ACME test** | klein | Throwaway domain oder LE staging CA. |
| **Rate-limit middleware in Traefik labels** | klein | 10 req/min/IP auf `/v1/*` + `/api/chat` failed-auth. |
| **Systemd timer für daily SQLite backup** | mittel | `.backup` + ship to B2/S3/local rsync. Service + timer unit + installer script. |
| **fail2ban jail für auth_rejected logs** | klein | Out-of-scope MVP aber worth scripting. |

### E. Developer experience

| Item | Größe | Notes |
|---|---|---|
| **Pre-commit hooks** | klein | `ruff format` + `ruff check` + `mypy` on staged files. Alle drei tools schon dep. |
| **`make help` Cleanup + `make deploy` Target** | klein | Nach A.1 frontend hand-off klar definiert. |

## Empfohlene Reihenfolge

1. **Pre-flight: #23 mergen** (sonst ist main broken).
2. **Frontend Quick-Wins**: Dark-mode + Empty-state + bessere Error-Toasts (~1h zusammen, alle drei lokal testbar).
3. **Conversation auto-title** server-side — sichtbar, kleines blast-radius.
4. **Mobile Layout** — der "I want to chat on the bus" UX-win.
5. **E2E /api/chat roundtrip test** — schließt die Test-Lücke bevor wir weitere features draufstapeln.
6. **Coverage in CI** — quick win, einmal eingebaut self-maintaining.
7. **External MCP client manager** — der nächste post-MVP Brocken, sobald Polish durch ist.

## Heads-up für die nächste Session

- **Lokales Stack läuft mit dem korrekten Stand** (claude 2.1.126 image, Specifyr-style OAuth), aber **github main ist broken** bis #23 gemerged ist. Wenn du ein PR vom main-aus aufmachst gehst du gegen die kaputte Basis.
- **CodeRabbit Rate-Limit**: 1 review/h auf der Orga. Re-trigger via `@coderabbitai review`.
- **Git push 403 von haex-space**: `gh auth switch --user haexhub`.
- **OAuth funktioniert** lokal getestet — `/settings/llm` → "OAuth starten" → claude.com authorize → Code paste → "Authorized" → Chat antwortet via Claude Max subscription.
- **Memory** ist aktualisiert: [[project-hermes-oauth-claude-2-1]] erklärt warum claude `>= 2.1.126` Pflicht ist, plus warum `setup-token` nicht der richtige Weg ist.

## Wenn du diesen Plan in einer neuen Session bekommst

Pip dir den Prompt aus `docs/plans/2026-05-25-prompt.md` (separate Datei, kurz gehalten für den Session-Start).
