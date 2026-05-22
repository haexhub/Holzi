# Session handoff — 2026-05-22 (late)

Read this when picking up the Hermes ([[project-hermes-agent]]) work in a new conversation. Memory under `/home/haex/.claude/projects/-home-haex-Projekte-Holzi/memory/` mirrors most of this; the deep what's-next breakdown lives in [`2026-05-22-roadmap.md`](./2026-05-22-roadmap.md).

## tldr

Phase 0–8 plus a stack of follow-ups are on `main`. The Nuxt frontend is in its own repo ([holzi-frontend](https://github.com/haexhub/holzi-frontend)) and is functionally complete for the MVP loop. The SQLAlchemy Core refactor that was queued in the memory is also done. Both repos have CI (pytest+ruff+mypy / nuxt-typecheck+vitest). No open PRs.

**Next item per the roadmap doc**: token-by-token streaming animation on the frontend (B.1), or A.1 frontend-handoff Dockerfile to unblock Phase 10 deploy. See the roadmap for the full ordered list.

## Branch state

Verify with `git fetch --prune && git log --oneline origin/main -n 10` before acting.

```
origin/main (Holzi, as of session end):
  686a571 ci: pytest + ruff + mypy on PRs and main (#12)
  507126a Repo layer on SQLAlchemy Core (async) (#11)
  3aed12b Tighten MessageResponse.role to Literal[user|assistant|tool] (#10)
  0e6f599 Backend: response_model= on /api endpoints (#9)
  85b0d8d Token-level SSE streaming on /api/chat (#8)
  f002ad5 Phase 8: web-UI backend (/api/*) with recursion-guarded tool catalog (#7)
  b8cfb4a docs: refresh handoff after Phase 7 merge
  433b8ce Phase 7: productivity + external tools, reminder scheduler (#6)

origin/main (holzi-frontend, separate repo):
  53af4b1 ci: nuxt typecheck + vitest on PRs and main (#4)
  7ddf713 Regen types after backend role-Literal tightening (#3)
  08bff4a Alias all API response types from the generated openapi schema (#2)
  d11d4ea openapi-typescript + Vitest (#1)
  e01fad2 Initial commit: Nuxt 4 + shadcn-vue SPA frontend for Hermes
```

No open PRs.

## What landed this session

See [`2026-05-22-roadmap.md`](./2026-05-22-roadmap.md) for the full per-PR breakdown. High-level:

- Phase 8 web-UI backend: `/api/chat` (SSE), `/api/conversations`, `/api/notes`, `/api/todos`, `/api/reminders`.
- Token-level SSE streaming on `/api/chat`, terminal-marker check on the upstream stream, cancellation cleanup on client disconnect.
- `response_model=` on every endpoint → named OpenAPI shapes the frontend's `openapi-typescript` codegen consumes.
- `MessageResponse.role` tightened to a `Literal` union → flows through to the frontend type.
- **SQLAlchemy Core refactor**: `app.state.db` is now an `AsyncEngine`. Each repo opens its own short-lived transaction via `engine.begin()`. `schema.py` owns the regular tables; `schema.sql` is FTS5-only. Connect-event listener applies `PRAGMA foreign_keys=ON` per pool checkout. Tests run on tmp_path file DBs (StaticPool would race the scheduler on `:memory:`).
- holzi-frontend: openapi-typescript pipeline, Vitest with 19 tests, all response/request types aliased from the generated schema.
- GitHub Actions CI on both repos.

## What still to do

The structured list is in [`2026-05-22-roadmap.md`](./2026-05-22-roadmap.md). Top picks for the next session:

1. **Frontend token-by-token streaming animation** (visual polish; backend already emits incremental events).
2. **`/api/chat` error semantics** (today every agent exception is a 200 + SSE `error` event).
3. **Frontend → backend hand-off** in the deploy Dockerfile so `docker compose up` ships a working stack.
4. **External MCP client manager** for haex-vault integration.

## Workflow reminders

- Active gh account: `haexhub`. Push fails to `haex-space` periodically → `gh auth switch --user haexhub` and retry. See [[feedback-pr-workflow-hermes]].
- CodeRabbit findings triage: [[feedback-coderabbit-skip-patterns]].
- No "Generated with Claude Code" / "Co-Authored-By: Claude" trailers in commits.
- Conversation: German. Code/commits/PR bodies: English.

## Tools / accounts / paths

- **Repo**: https://github.com/haexhub/Holzi
- **Frontend repo**: https://github.com/haexhub/holzi-frontend (private, haexhub)
- **Working dir**: `/home/haex/Projekte/Holzi` and `/home/haex/Projekte/holzi-frontend`
- **Backend**: `uv` for installs, `uv run pytest -q`, `uv run ruff check src tests`, `uv run mypy src`.
- **Frontend**: `pnpm`, Node 22. `pnpm test`, `pnpm typecheck`, `pnpm run gen:api` (regen types from running hermes).
- **Design doc**: `docs/plans/2026-05-21-hermes-mvp-design.md`.
- **Roadmap**: `docs/plans/2026-05-22-roadmap.md` (this session's output).
- All conventions: global `~/.claude/CLAUDE.md` plus [[feedback-no-vpn]], [[feedback-coderabbit-skip-patterns]], [[feedback-pr-workflow-hermes]].
