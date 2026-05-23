# Session handoff — 2026-05-23 evening

Read this when picking up Hermes ([[project-hermes-agent]]) work in a new conversation. Memory under `/home/haex/.claude/projects/-home-haex-Projekte-Holzi/memory/` mirrors most of this; the per-task breakdown lives in [`2026-05-24-next-session.md`](./2026-05-24-next-session.md).

## tldr

Die LLM-Credentials-Feature ist Phase 1–7 auf `main`, plus der Dockerfile-Fix (#18). Zwei PRs warten auf den nächsten Tag:

| PR | Was | Blockiert von |
|---|---|---|
| [Holzi #19](https://github.com/haexhub/Holzi/pull/19) | Per-Credential `model`-Spalte + `GET /credentials/{id}/models` + `PATCH /credentials/{id}/model`; Agent-Loop nutzt `cred.model` mit `settings.model`-Fallback | — |
| [holzi-frontend #8](https://github.com/haexhub/holzi-frontend/pull/8) | `ModelSelect.vue` Combobox (reka-ui) per Credential-Row | Holzi #19 |

**Start here:** #19 mergen (CodeRabbit triagen via [[feedback-coderabbit-skip-patterns]]), dann #8, dann Smoke-Test der UI mit echtem OAuth.

## Branch state

Verify with `git fetch --prune && git log --oneline origin/main -n 8` before acting.

```
origin/main (Holzi, last merged at end-of-session):
  d28a5a1 fix(docker): install Node + claude CLI in hermes-server image (#18)
  b81ad40 feat: local dev-stack uses the sqlite credential resolver (#17)
  04f0606 feat: agent loop reads the active DB credential (#16)
  bb28af0 feat: claude OAuth subprocess flow (4 endpoints) (#15)
  606cd2b feat: llm_credentials backend (schema + crypto + CRUD API) (#14)

origin/main (holzi-frontend):
  fa072a9 feat: /settings/llm page (#7)
  + earlier streaming/pnpm-workspace/CI PRs

Open PRs (alle haexhub-account):
  Holzi#19           feat: per-credential model + GET /credentials/{id}/models
  holzi-frontend#8   feat: per-credential model picker (searchable combobox)
```

## What landed this session

1. **PR #14–#17 gemerged** — LLM-credentials feature komplett (Phasen 1–7).
2. **Phase 5 — resolver-sqlite plugin** im neuen Repo `haex-claude-proxy-resolver-sqlite` (PR #1 gemerged, kein CI dort).
3. **Phase 6 — Frontend `/settings/llm`** Page mit API-Key-Form + OAuth-State-Machine + Settings-Link im Chat-Header.
4. **Smoke-Test gestartet** mit `make up-local-full`. OAuth scheiterte mit 500 → Root-Cause: `claude` CLI fehlte im `hermes-server`-Image. Fix in PR #18.
5. **Per-Credential `model`-Feature** spec'd vom User (analog Specifyrs `ModelSelect.vue`). Backend in #19, Frontend in #8.
6. **Naming-Fix** — überall "Marko" → "Martin" (User-Memory, System-Prompts, Test-Fixtures, UI-Placeholder, Git-Configs).

## What still to do (Reihenfolge)

Siehe [`2026-05-24-next-session.md`](./2026-05-24-next-session.md) für den per-task-Plan. Highlights:

1. **#18 mergen** (Dockerfile fix — risikoarm)
2. **#19 mergen** nach CodeRabbit-Triage
3. **#8 mergen** nach #19 + CodeRabbit
4. **Manueller End-to-End-Smoke** der vollen UI — OAuth + API-Key + Model-Dropdown auf http://app.localhost:11080/settings/llm
5. Optional: **PR #19's 10-min Model-Cache** überdenken — Specifyr hat denselben Cache, könnte für single-user zu lang sein (Cache-Invalidate auf `PATCH /model`?)

**Danach Roadmap-Items aus `docs/plans/2026-05-22-roadmap.md` Section B/C/D:**
- `/api/chat` Error-Semantik (502/504 vs 500)
- Conversation auto-title aus erster Message
- Dark-mode Toggle
- Mobile Layout
- SSE Reconnect on drop
- E2E `/api/chat` → agent → tool Test
- Coverage in CI

## Workflow reminders

- Active gh-Account: `haexhub`. Push fällt zurück auf `haex-space`, dann `gh auth switch --user haexhub`. Siehe [[feedback-pr-workflow-hermes]].
- CodeRabbit-Findings triagen gegen [[feedback-coderabbit-skip-patterns]].
- CodeRabbit Rate-Limit: 1 Review/Stunde auf der Orga. Re-trigger via `@coderabbitai review` als PR-Comment, ~3–4 min Turnaround.
- Keine "Generated with Claude Code" / "Co-Authored-By: Claude" Trailer in Commits.
- Konversation: Deutsch. Code/commits/PR-Bodies: Englisch.
- Git-Config in `Holzi/` und `holzi-frontend/` ist auf `Martin Drechsel <mdrechsel@itemis.com>` gesetzt — bitte so lassen.

## Tools / accounts / paths

- **Repo**: https://github.com/haexhub/Holzi
- **Frontend repo**: https://github.com/haexhub/holzi-frontend (private)
- **Plugin repo**: https://github.com/haexhub/haex-claude-proxy-resolver-sqlite (private)
- **Working dirs**: `/home/haex/Projekte/Holzi`, `/home/haex/Projekte/holzi-frontend`, `/home/haex/Projekte/haex-claude-proxy-resolver-sqlite`
- **Backend**: `uv run pytest`, `uv run ruff check src tests`, `uv run mypy src`. 248 tests grün auf `feat/credential-model-column` (PR #19).
- **Frontend**: `pnpm test` (30 tests), `pnpm typecheck`, `pnpm run gen:api` (regen nach Backend-Änderung). PR #8 30/30 grün.
- **Local dev-stack**: `make up-local-full` → backend + frontend + proxy + traefik auf `*.localhost:11080`. `.env` muss `HERMES_AUTH_TOKEN` + `HERMES_SECRET_KEY` (64-hex) gesetzt haben.
- **Design docs**: `docs/plans/2026-05-21-hermes-mvp-design.md`, `docs/plans/2026-05-23-llm-credentials-design.md`.
- **Next-session plan**: [`2026-05-24-next-session.md`](./2026-05-24-next-session.md).
- Conventions: `~/.claude/CLAUDE.md` plus [[feedback-no-vpn]], [[feedback-coderabbit-skip-patterns]], [[feedback-pr-workflow-hermes]].
