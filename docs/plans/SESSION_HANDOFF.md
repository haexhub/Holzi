# Session handoff — 2026-05-23

Read this when picking up the Hermes ([[project-hermes-agent]]) work in a new conversation. Memory under `/home/haex/.claude/projects/-home-haex-Projekte-Holzi/memory/` mirrors most of this; the deep what's-next breakdown lives in [`2026-05-23-next-session.md`](./2026-05-23-next-session.md).

## tldr

LLM-credentials feature in flight. Phases 1 + 2 (schema + AES-GCM helper + CRUD API) sit on the open PR [Holzi#14](https://github.com/haexhub/Holzi/pull/14). Phases 3-7 (OAuth flow, agent integration, resolver plugin, frontend UI, compose update) are spec'd in [`2026-05-23-next-session.md`](./2026-05-23-next-session.md) and [`2026-05-23-llm-credentials-design.md`](./2026-05-23-llm-credentials-design.md).

Local dev-stack via `docker-compose.local.yml` is on main. Browser-accessible Hermes UI under `http://app.localhost:11080/` after `make up-local-full`. Currently the proxy bind-mounts `~/.claude` — that bind-mount goes away once Phases 3-7 are done.

**Start here**: confirm PR #14 is in good shape, merge it, then jump to Phase 3 (Claude OAuth flow in the backend).

## Branch state

Verify with `git fetch --prune && git log --oneline origin/main -n 8` before acting.

```
origin/main (Holzi, as of session end):
  639263d docs: next-session plan for the LLM-credentials feature
  bb1211e feat: local dev-stack via docker-compose.local.yml (#13)
  8050660 docs: 2026-05-22 roadmap + session-handoff refresh
  686a571 ci: pytest + ruff + mypy on PRs and main (#12)
  507126a Repo layer on SQLAlchemy Core (async) (#11)
  3aed12b Tighten MessageResponse.role to Literal[user|assistant|tool] (#10)

origin/main (holzi-frontend):
  a9d8c68 feat: live token streaming in chat bubble (#5)
  7fe6d37 build: add pnpm-workspace.yaml with onlyBuiltDependencies (#6)
  53af4b1 ci: nuxt typecheck + vitest on PRs and main (#4)

Open PRs:
  Holzi#14  feat: llm_credentials backend (schema + crypto + CRUD API)
            branch feat/llm-credentials — phases 1+2 of the credentials feature
```

## What landed this session

- holzi-frontend#5 — live token-by-token streaming animation in the chat bubble.
- holzi-frontend#6 — `pnpm-workspace.yaml` whitelisting esbuild / @parcel/watcher / vue-demi for postinstall builds (Docker dev container was crashlooping without it).
- Holzi#13 — `docker-compose.local.yml` with bundled Traefik routing `*.localhost`, a `frontend` profile that brings up the holzi-frontend Nuxt-dev container, and new `make up-local{,-full}` targets. Two CodeRabbit findings addressed (image-tag doc fix, loopback bind).
- Holzi#14 (open) — phases 1+2 of the LLM-credentials feature. AES-256-GCM helper, `llm_credentials` table + stable `proxy_credentials_v1` view, CRUD endpoints under `/api/llm/credentials/`. 15 new tests, full suite 194 green, ruff + mypy clean.
- haex-claude-proxy — local `:dev` image rebuilt from the now-merged generic-resolver refactor (PR #5 there, merged 2026-05-21). Pre-rebuild image was emitting 503s due to the old hardcoded PG resolver.

## What still to do

Two layers:

**1. Finish the LLM-credentials feature** — [`2026-05-23-next-session.md`](./2026-05-23-next-session.md) has the per-phase breakdown with files-to-touch and gotchas. Phases 3-7 in strict order, realistic session budget ~3 sessions:

- Phase 3: Claude OAuth flow (backend subprocess driver + 4 endpoints)
- Phase 4: Agent loop reads the active DB credential (env vars stay as fallback)
- Phase 5: New repo `haex-claude-proxy-resolver-sqlite` — npm plugin for the proxy
- Phase 6: Frontend `/settings/llm` page + OAuth modal
- Phase 7: Drop the `~/.claude` bind-mount, share `hermes-data` volume with the proxy

**2. Roadmap items not yet started** — [`2026-05-22-roadmap.md`](./2026-05-22-roadmap.md) Section B/C/D:

- `/api/chat` error semantics (502/504 vs 500 distinction)
- Conversation auto-title from the first message
- Dark-mode toggle (CSS vars are in place)
- Mobile layout (today fixed `grid-cols-[260px_1fr_320px]`)
- SSE reconnect on drop
- E2E integration test for `/api/chat` → agent → tool round-trip
- Coverage reporting in CI

## Workflow reminders

- Active gh account: `haexhub`. Push fails to `haex-space` periodically → `gh auth switch --user haexhub` and retry. See [[feedback-pr-workflow-hermes]].
- CodeRabbit findings triage: [[feedback-coderabbit-skip-patterns]].
- No "Generated with Claude Code" / "Co-Authored-By: Claude" trailers in commits.
- Conversation: German. Code/commits/PR bodies: English.

## Tools / accounts / paths

- **Repo**: https://github.com/haexhub/Holzi
- **Frontend repo**: https://github.com/haexhub/holzi-frontend (private, haexhub)
- **Working dir**: `/home/haex/Projekte/Holzi` and `/home/haex/Projekte/holzi-frontend`
- **Backend**: `uv` for installs, `uv run pytest`, `uv run ruff check src tests`, `uv run mypy src`.
- **Frontend**: `pnpm`, Node 22. `pnpm test`, `pnpm typecheck`, `pnpm run gen:api` (regen types from running hermes).
- **Local dev-stack**: `make up-local-full` brings up backend + frontend + proxy + traefik on `*.localhost:11080`. See [`docker-compose.local.yml`](../../docker-compose.local.yml) header for prerequisites (`.env`, `haex-claude-proxy:dev` image, sibling `holzi-frontend` checkout).
- **Design docs**: `docs/plans/2026-05-21-hermes-mvp-design.md`, `docs/plans/2026-05-23-llm-credentials-design.md`.
- **Next-session plan**: [`docs/plans/2026-05-23-next-session.md`](./2026-05-23-next-session.md).
- All conventions: global `~/.claude/CLAUDE.md` plus [[feedback-no-vpn]], [[feedback-coderabbit-skip-patterns]], [[feedback-pr-workflow-hermes]].
