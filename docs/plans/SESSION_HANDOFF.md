# Session handoff — 2026-05-22

Read this when picking up the Hermes ([[project-hermes-agent]]) work in a new conversation. Memory under `/home/haex/.claude/projects/-home-haex-Projekte-Holzi/memory/` already mirrors most of this, but this document is the single-page snapshot.

## Where things stand right now

Branch state (verify with `git fetch --prune && git log --oneline origin/main -n 10` before acting):

```
origin/main (latest):
  9ff3343 Phase 6: MCP server with memory + cross-channel tools (#5)
  106f157 Phase 5: Agent loop with tool-use (#4)
  26917ce Phase 4: Signal worker (#3)
  8bb544e Phase 3: OpenAI-compatible LLM proxy (#2)
  69e77e0 Merge pull request #1 from haexhub/phase-2-memory-layer
  7285e3b Address code review on PR #1
  a11cfa5 Phase 2: SQLite memory layer
  a4849fa Phase 1: hermes-server skeleton
  39ad140 Phase 0: project skeleton

Open branch:
  phase-7-productivity-external   PR #6 → main
```

**Phase 0–6 are on `main`.** **PR #6 (Phase 7)** is open and stuck waiting on CodeRabbit. The rate-limit window resets around **2026-05-22T10:43:00Z**. Last successful operation: phase-7 branch pushed, PR opened, CodeRabbit response was a rate-limit warning (41 minutes wait).

There is **no active background watcher** anymore (stopped before handoff).

## Pick up the PR #6 review

1. `cd /home/haex/Projekte/Holzi`
2. `gh auth status` — verify active account is `haexhub`, not `haex-space`. If wrong, `gh auth switch --user haexhub`. (Pattern documented in [[feedback-pr-workflow-hermes]].)
3. Check whether the rate-limit window has passed. If not, wait it out — re-triggering before the reset extends the clock.
4. `gh pr comment 6 --body "@coderabbitai review"` to trigger.
5. Poll for the review:
   ```bash
   until [[ "$(gh api repos/haexhub/Holzi/pulls/6/reviews | jq '. | length')" -gt 0 ]]; do
     sleep 30
   done
   ```
   Don't be fooled by the `"Review triggered"` issue-comment — that's the acknowledgement, not the review. The real review shows up as an entry in `pulls/6/reviews`.
6. Walk through the actionable + nitpick findings against [[feedback-coderabbit-skip-patterns]]. Apply valid ones. Skip the rest with a one-line reason in the commit body.
7. `git push origin phase-7-productivity-external`. CodeRabbit re-reviews automatically; wait for `statusCheckRollup[0].state == "SUCCESS"`.
8. `gh pr merge 6 --squash --delete-branch=false` and `git pull origin main` locally.

## What PR #6 contains (summary)

- 7 new tools added to the catalogue: `reminder_set`, `reminder_list`, `todo_add`, `todo_list`, `todo_done`, `web_search` (Brave), `url_fetch` (trafilatura). Total catalogue size is now **14**.
- New tables in `schema.sql`: `reminders`, `todos` (both with partial indexes on open/pending rows).
- New repos: `hermes.repository.reminders`, `hermes.repository.todos`.
- New module `hermes.scheduler` — `ReminderScheduler` asyncio loop, polls every 60 s, fires via the existing `SignalClient`.
- New `app.state.external_http` (separate `httpx.AsyncClient`) so a broken Brave key can't poison the LLM client.
- New setting `HERMES_BRAVE_API_KEY` (optional). `.env.example` documents it.
- New dependency: `trafilatura>=1.12` (sync, called via `asyncio.to_thread`).
- 115/115 tests green, ruff clean, container builds.

## After PR #6 is merged → Phase 8

`docs/plans/2026-05-21-hermes-mvp-design.md` §5 Phase 8:

- `/api/chat` (REST + SSE) — streaming agent responses for the web UI
- `/api/conversations`, `/api/notes`, `/api/todos`, `/api/reminders` (REST CRUD)
- **First internal caller that hands `app.state.tool_catalog` to `run_agent`.** Recursion guard needed: prevent `cross_channel_send` to a Signal channel when the agent itself was triggered from a Signal envelope (Signal-worker keeps `tools=None` for now; web-UI gets full tool access). Encode the guard either by filtering the catalogue per request or by adding a "current channel" context to `cross_channel_send` so it refuses to send to the same channel.

Workflow for Phase 8:
1. `git checkout main && git pull` (after #6 merges).
2. `git switch -c phase-8-web-ui-backend`.
3. Branch off; same TDD + per-tool test pattern as Phases 5–7.
4. PR base = `main`, no stacking needed since #6 will be merged by then.

## Tools / accounts / paths

- Repo: https://github.com/haexhub/Holzi (active gh account: `haexhub`).
- Working dir: `/home/haex/Projekte/Holzi`.
- Python: `uv` is the package manager. `uv sync --extra dev` to install. `uv run pytest -q`, `uv run ruff check src tests`.
- Container: `docker compose -p hermes build hermes-server`.
- Design doc: `docs/plans/2026-05-21-hermes-mvp-design.md` (§6.7 has the phase-status table).
- All conventions: global `~/.claude/CLAUDE.md` plus [[feedback-no-vpn]], [[feedback-coderabbit-skip-patterns]], [[feedback-pr-workflow-hermes]].

## One-line summary for the next session

> Phase 0–6 merged. PR #6 (Phase 7) open, awaiting CodeRabbit (rate-limit reset ~10:43 UTC). Retrigger review, apply valid findings per [[feedback-coderabbit-skip-patterns]], merge, then start Phase 8 (web-UI backend, `/api/chat` with streaming + tool-catalogue access for the agent).
