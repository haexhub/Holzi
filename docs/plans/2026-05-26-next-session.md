# Next session — 2026-05-26

Letzte Session (2026-05-24/25) hat Hermes auf `holzi.haex.cloud` produktiv gemacht und die UI-driven Signal-Linking-Flow gebaut. Lies erst `docs/plans/SESSION_HANDOFF.md` für den vollen State, dann pick ein Item.

## Was gerade lief

Session 2026-05-24/25, ehrlich aufgearbeitet:

| Bereich | Was |
|---|---|
| Production-Deploy | Ansible-Rollen + Watchtower (nickfedor-Fork), Image-Pipelines pro Repo |
| Path-Routing | Traefik scored `Host && PathPrefix` höher, Backend gewinnt API-Pfade, SPA fängt Rest |
| Per-Tenant Proxy | Holzi hat eigenen `holzi-claude-proxy` mit resolver-sqlite, Specifyr unverändert |
| Settings-UI | Tabs-Layout via Nested-Routes (`pages/settings.vue` + `pages/settings/{llm,messenger}.vue`) |
| Signal-Linking | QR-PNG-Endpoint + Polling, ersetzt `docker exec signal-cli link` |
| Schema | `messenger_accounts` Tabelle für signal + telegram unter einem Schema |
| Hot-Reload Worker | `signal/lifecycle.py` mit `rebuild_signal_worker_from_db` Pattern |

**Was nicht passiert ist (bewusst):**
- WhatsApp — out wegen Meta-ToS-Banrisiko
- Authentik-Forward-Auth — bewusst rückgebaut, würde Bearer-Login der SPA doppeln
- Alembic — bestehender `metadata.create_all` + lightweight-migrations Pattern reicht für additive Changes

## Pre-flight

1. `git fetch --prune` in allen drei Repos (`Holzi`, `holzi-frontend`, `haex-claude-proxy-resolver-sqlite`) + `ansible`. Alles sollte sauber sein, keine offenen PRs.
2. Production-Status checken: `curl -s https://holzi.haex.cloud/healthz` muss 200 + `{"status":"ok"}` liefern.
3. Optional: lokal `make up-local-full` falls am Backend/Frontend gearbeitet wird.

## Roadmap (offen, prio-sortiert)

### A. Phase 3: Telegram-Bot-Integration

Größtes Item, klare Story. Schema ist schon da (`bot_username` + `bot_token_iv/tag/data` + `allowed_chat_ids` Spalten in `messenger_accounts`), nur Routes + Worker fehlen.

**Backend** (PR im Holzi-Repo):

| Subtask | Größe | Notes |
|---|---|---|
| `POST /api/messenger/accounts/telegram` | klein | Body: `{ bot_token: str }`. Backend ruft Telegram's `getMe` mit dem Token → extrahiert `bot_username` zur Anzeige → AES-GCM-encrypt Token → DB-Insert. 400 wenn `getMe` fehlschlägt. |
| `hermes/messenger/telegram/{client,worker}.py` | mittel | Mirror der `signal/`-Struktur. `TelegramClient` macht Long-Polling `getUpdates`. Worker dispatched eingehende Messages an den gleichen `agent_runner_factory` wie Signal. |
| `messenger/lifecycle.py` (rename von `signal/lifecycle.py`) | klein | Generalisieren: `rebuild_messenger_workers_from_db()` für signal UND telegram. Aufrufer in Routes + Lifespan anpassen. |
| `allowed_chat_ids` Filter | klein | Worker nimmt nur Messages aus dieser ID-Liste an. NULL = jeder Chat. |
| `schema.py`: `channel` enum erweitern | trivial | `'signal'|'web'|'vscode'` → `'signal'|'web'|'vscode'|'telegram'`. Conversation-Rows mit telegram-channel tracken. |

**Frontend** (PR im holzi-frontend-Repo):

| Subtask | Größe | Notes |
|---|---|---|
| Telegram-Section in `pages/settings/messenger.vue` | klein | Bot-Token-Input-Field + "@BotFather"-Link + Submit-Button. Optional: Allowed-Chat-IDs Multi-Input. |
| `useMessenger.createTelegram` Composable | trivial | `api.post('/api/messenger/accounts/telegram', { bot_token })`. |
| List-Section um Telegram erweitern | klein | Aktuell filtert nur auf `signal` — `signalAccounts` umbenennen, plus `telegramAccounts` Computed. Listen-Item zeigt `@bot_username`. |

**Tests**:
- `tests/test_api_messenger.py` um Telegram-Path erweitern (httpx.MockTransport für `getMe`)
- Worker-Tests mit Mock-Updates

### B. Frontend Polish (alle unabhängig, pick was Zeit gibt)

| Item | Größe | Notes |
|---|---|---|
| **Conversation Auto-Title aus erster Message** | mittel | Backend leitet bei `conversation_id == None` den Titel aus `payload.message[:40]` ab. + `PATCH /conversations/{id}` Endpoint für Rename. Frontend zeigt Titel in der Sidebar. |
| **Mobile Layout (3-Spalten-Collapse)** | mittel | `app/pages/index.vue` ist `grid-cols-[260px_1fr_320px]` fix. Auf `<md` → Drawer für Sidebar + Right-Panel als Tabs. Tailwind + reka-ui Sheet. |
| **SSE Reconnect on drop** | mittel | `useChatStream.ts` braucht retry-with-backoff. `onerror` event, auto-reopen mit Backoff (1s, 2s, 4s, …, max 30s). State-machine: `'streaming'|'reconnecting'|'failed'`. |
| **Token-by-Token Typing-Animation** | klein | Backend streamt schon `text` events incremental. Chat-View rendert aktuell vollständig — auf incremental rendering umstellen. |

### C. Backend Polish

| Item | Größe | Notes |
|---|---|---|
| **E2E `/api/chat` → agent → tool roundtrip Test** | mittel | pytest-fixture mit mock upstream, assert auf message-history mit user→assistant(tool_call)→tool→assistant turns. |
| **Coverage Reporting in CI** | klein | `pytest --cov` + Codecov action. Vitest `--coverage` für Frontend. |
| **`conn` → `engine` rename** | klein | Test-Fixture Name. Single sed-sweep auf ~15 test-files. |

### D. Operations

| Item | Größe | Notes |
|---|---|---|
| **Branches in ansible aufräumen** | trivial | `chore/holzi-subdomain`, `chore/watchtower-fork`, `feat/holzi-authentik`, `feat/holzi-claude-proxy-sidecar`, `feat/holzi-frontend`, `feat/holzi-role`, `fix/watchtower-maintained-fork` etc. — alle squash-merged. `git branch -d <name>` jeweils. |
| **Production SSL-Renewal monitoring** | klein | Let's-Encrypt-Renewals laufen über Traefik. Optional: Healthcheck-Cron der Cert-Expiry tracked. |
| **Backup-Strategie für `hermes-data` Volume** | mittel | SQLite-DB liegt im Docker-Volume. Bisher kein Backup-Schema. Optional: systemd-Timer der `sqlite3 .backup` macht + zu nem Off-Site-Bucket schiebt. |

### E. Bigger picture (Post-Telegram)

| Item | Größe | Notes |
|---|---|---|
| **External MCP Clients** | groß | haex-vault als ersten MCP-Client integrieren. `HERMES_EXTERNAL_MCP` env-var existiert schon als JSON-Array, aber Hermes ruft die noch nicht auf. |
| **Browser-Passkey statt Text-Token** | groß | WebAuthn als Alternative zum text-token-paste in `/login`. Backend braucht Passkey-Storage. |
| **Multi-Account pro Messenger** | mittel | Partial Unique Index in schema.sql droppen + UI-Mehrfach-Account-Liste. Worker-Pool statt single-worker. Heute artificially auf single-account beschränkt. |

## How to apply

- Empfehlung: **Item A (Telegram) als Stack** — ein Backend-PR, ein Frontend-PR, gemeinsam mergen. Phase 3 abschließen.
- Alternative wenn weniger Zeit: ein einzelnes Item aus B oder C als Quick-Win (~1h).
- Vermeide gleichzeitiges Anpassen mehrerer Layer (Backend + Frontend + Ansible) in einem PR — die etablierte Praxis ist ein PR pro Repo, gestackt wenn Reihenfolge wichtig ist (siehe [[feedback-pr-workflow-hermes]]).
