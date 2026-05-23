# Next session — 2026-05-24

Letzte Session (2026-05-23) hat die LLM-Credentials-Feature Phasen 1–7 gemerged und drei Folge-PRs gepusht. Lies zuerst [`SESSION_HANDOFF.md`](./SESSION_HANDOFF.md) — dieses Doc ist die per-task-Auflösung davon.

## Reihenfolge

PR #18 (Dockerfile-Fix) wurde noch in der Vorsession gemerged (`d28a5a1`). Aktuelle Reihenfolge startet mit #19.

### 1. Holzi #19 — per-credential model column

PR: https://github.com/haexhub/Holzi/pull/19

Backend für den Model-Picker auf `/settings/llm`:
- Neue Spalte `model TEXT` auf `llm_credentials` (additive Migration in `db.py`)
- `GET /api/llm/credentials/{id}/models` ruft Provider-`/v1/models` ab (OpenAI + OpenRouter mit Bearer, Anthropic mit `x-api-key`, Google mit `?key=`, Anthropic-OAuth nimmt curated Liste)
- `PATCH /api/llm/credentials/{id}/model` schreibt das gewählte Modell
- Agent-Loop (`/api/chat` + Signal worker) liest `cred.model` mit `settings.model`-Fallback
- 10-min in-memory Cache pro `(provider, cred_id)` für die Provider-Liste

19 neue Tests, 248/248 grün, ruff + mypy clean.

- [ ] CodeRabbit-Findings triagen
- [ ] Merge

### 2. holzi-frontend #8 — ModelSelect Combobox

PR: https://github.com/haexhub/holzi-frontend/pull/8

Wired die Modell-Auswahl ins UI:
- Neues Component `ModelSelect.vue` direkt auf `reka-ui` `Combobox` (shadcn-CLI hat den `shadcn-nuxt`-Config nicht akzeptiert)
- `useLlmCredentials` bekommt `setModel(id, value | null)` + `listModels(id)`
- In `settings/llm.vue` pro Credential-Row eingebaut mit optimistic update
- 3 neue Vitest-Cases (30/30 grün), typecheck clean

- [ ] CodeRabbit-Findings triagen
- [ ] Merge nach #19 (Frontend braucht die neuen Endpoints)
- [ ] Manueller End-to-End-Smoke siehe Schritt 4

### 3. Manueller End-to-End-Smoke-Test

Voraussetzung: #19 + #8 gemerged. Das Image hat seit #18 den `claude`-CLI an Bord.

1. `make up-local-full` aus `/home/haex/Projekte/Holzi`
2. http://app.localhost:11080 → Login mit `HERMES_AUTH_TOKEN`
3. "Settings" → `/settings/llm`
4. **Variante A — OAuth**:
   - "OAuth starten" → Tab öffnet sich → bei Anthropic authorisieren → Code zurück → submit
   - Liste zeigt "Aktiv" + ModelSelect mit curated Claude-Models (Opus 4.7, Sonnet 4.6, Haiku 4.5)
   - Modell wählen → Chat-Tab → Nachricht senden → response sollte mit dem gewählten Modell ankommen
5. **Variante B — OpenAI API key**:
   - Provider "openai" + API-Key + Display-Name → "Hinzufügen"
   - "Aktivieren" → ModelSelect fetched echte Liste über `/v1/models`
   - Modell wählen, Chat testen
6. Logs prüfen wenn was schief geht:
   - `docker compose -f docker-compose.local.yml logs hermes-server haex-claude-proxy`

### 4. Optional: Model-Cache-Invalidate beim Modell-Schreiben

Specifyr's 10-min Cache invalidiert nur per TTL. Das ist für single-user OK, aber wenn der User ein neues Modell beim Provider hinzufügt, sieht er's erst nach 10 min. Klein:
- `PATCH /credentials/{id}/model` → `clear_cache(cred_id)` aufrufen
- ggf. ein Button "Model-Liste neu laden" im UI

Nice-to-have, nicht Block.

## Danach: Roadmap-Items

Aus `docs/plans/2026-05-22-roadmap.md` Section B/C/D, alle unabhängig — pick eins:

| Item | Größe | Wo |
|---|---|---|
| `/api/chat` Error-Semantik (502/504 vs 500) | klein | `routes/chat.py` + `routes/api.py` |
| Conversation auto-title aus erster Message | mittel | neuer Hook in `messages.append`, Agent-Call mit ≤30-token-Budget |
| Dark-mode Toggle | klein | CSS-Vars existieren, `useColorMode` von `@vueuse/core` |
| Mobile Layout | mittel | heute `grid-cols-[260px_1fr_320px]` fix → responsive collapse |
| SSE Reconnect on drop | mittel | `useChatStream` braucht retry-with-backoff |
| E2E `/api/chat` → agent → tool Test | mittel | pytest-fixture für Mock-LLM, asserts auf DB-state |
| Coverage Reporting in CI | klein | `pytest --cov` + Codecov action in `.github/workflows/ci.yml` |

## Heads-up

- **Smoke-Test scheitert ohne `HERMES_SECRET_KEY`** in `.env` (64-hex, `openssl rand -hex 32`). Wenn das Volume `holzi_hermes-data` schon existiert und der Key dort generiert wurde, muss er aus `master.key` im Volume gezogen werden — sonst kann die DB nicht entschlüsselt werden.
- **PR #19 hat einen amend + force-push** wegen des "Marko"→"Martin"-Rename-Fixes mit korrektem Author. CodeRabbit re-reviewed wahrscheinlich, ggf. erneut triggern.
- **resolver-sqlite hat kein CI**, das Plugin ist nur lokal getestet (18 node:test Cases). Falls Image neu gebaut werden muss: `cd /home/haex/Projekte/haex-claude-proxy-resolver-sqlite && npm install`.
