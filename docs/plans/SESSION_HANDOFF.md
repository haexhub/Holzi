# Session handoff — 2026-05-22

Read this when picking up the Hermes ([[project-hermes-agent]]) work in a new conversation. Memory under `/home/haex/.claude/projects/-home-haex-Projekte-Holzi/memory/` already mirrors most of this, but this document is the single-page snapshot.

## Where things stand right now

Branch state (verify with `git fetch --prune && git log --oneline origin/main -n 10` before acting):

```
origin/main (latest):
  433b8ce Phase 7: productivity + external tools, reminder scheduler (#6)
  9ff3343 Phase 6: MCP server with memory + cross-channel tools (#5)
  106f157 Phase 5: Agent loop with tool-use (#4)
  26917ce Phase 4: Signal worker (#3)
  8bb544e Phase 3: OpenAI-compatible LLM proxy (#2)
  69e77e0 Merge pull request #1 from haexhub/phase-2-memory-layer
  7285e3b Address code review on PR #1
  a11cfa5 Phase 2: SQLite memory layer
  a4849fa Phase 1: hermes-server skeleton
  39ad140 Phase 0: project skeleton

No open PRs.
```

**Phase 0–7 are on `main`.** PR #6 was squash-merged at 2026-05-22T11:05:49Z after one CodeRabbit round (4 findings fixed, 4 skipped with reasoning, 3 new tests; 118/118 green).

## What landed in Phase 7 (catalogue size now **14**)

- 7 new tools: `reminder_set`, `reminder_list`, `todo_add`, `todo_list`, `todo_done`, `web_search` (Brave), `url_fetch` (trafilatura).
- New tables: `reminders`, `todos` (partial indexes on open/pending rows).
- New repos: `hermes.repository.reminders`, `hermes.repository.todos`.
- New module `hermes.scheduler` — `ReminderScheduler` asyncio loop, polls every 60 s, fires via the existing `SignalClient`.
- Separate `app.state.external_http` so a broken Brave key can't poison the LLM client.
- New setting `HERMES_BRAVE_API_KEY` (optional). `.env.example` documents it.
- New dependency: `trafilatura>=1.12` (sync, called via `asyncio.to_thread`).
- Review-fix commit hardened the lifespan teardown (None-init + guarded cleanup), added SSRF guard on `url_fetch` (scheme + IP-literal block-list, no DNS resolve), replaced an `assert` in `todo_done` with an explicit error, and added the `AND fired_at IS NULL` guard to `reminders.mark_fired`.

## Next up → Phase 8

`docs/plans/2026-05-21-hermes-mvp-design.md` §5 Phase 8:

- `/api/chat` (REST + SSE) — streaming agent responses for the web UI.
- `/api/conversations`, `/api/notes`, `/api/todos`, `/api/reminders` (REST CRUD, thin wrappers over the existing repos).
- **First internal caller that hands `app.state.tool_catalog` to `run_agent`.** Recursion guard needed: prevent `cross_channel_send` to a Signal channel when the agent itself was triggered from a Signal envelope (Signal-worker keeps `tools=None` for now; web-UI gets full tool access). Encode the guard either by filtering the catalogue per request or by adding a "current channel" context to `cross_channel_send` so it refuses to send to the same channel.

Workflow:
1. `cd /home/haex/Projekte/Holzi && git checkout main && git pull`.
2. `gh auth status` — confirm active account is `haexhub` (see [[feedback-pr-workflow-hermes]]).
3. `git switch -c phase-8-web-ui-backend`.
4. Same TDD + per-endpoint test pattern as Phases 5–7.
5. PR base = `main`, no stacking.
6. CodeRabbit findings → triage against [[feedback-coderabbit-skip-patterns]].

## Tools / accounts / paths

- Repo: https://github.com/haexhub/Holzi (active gh account: `haexhub`).
- Working dir: `/home/haex/Projekte/Holzi`.
- Python: `uv` is the package manager. `uv sync --extra dev` to install. `uv run pytest -q`, `uv run ruff check src tests`.
- Container: `docker compose -p hermes build hermes-server`.
- Design doc: `docs/plans/2026-05-21-hermes-mvp-design.md` (§6.7 has the phase-status table).
- All conventions: global `~/.claude/CLAUDE.md` plus [[feedback-no-vpn]], [[feedback-coderabbit-skip-patterns]], [[feedback-pr-workflow-hermes]].

## One-line summary for the next session

> Phase 0–7 merged. Start Phase 8 (web-UI backend): `/api/chat` with SSE streaming + REST CRUD for conversations/notes/todos/reminders, first internal handover of `app.state.tool_catalog` to `run_agent` with a `cross_channel_send` recursion guard. Branch `phase-8-web-ui-backend` off `main`.
