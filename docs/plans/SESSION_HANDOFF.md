# Session handoff — 2026-05-24 evening

Read this when picking up Hermes ([[project-hermes-agent]]) work in a new conversation. Per-task plan lives in [`2026-05-25-next-session.md`](./2026-05-25-next-session.md); session-start prompt in [`2026-05-25-prompt.md`](./2026-05-25-prompt.md).

## tldr

Chat funktioniert end-to-end via Claude Max OAuth. Eine Fix-PR ist offen — die muss zuerst rein, dann ist `main` wieder sauber.

| PR | Repo | Was | State |
|---|---|---|---|
| [Holzi #23](https://github.com/haexhub/Holzi/pull/23) | Holzi | claude-code pin 2.1.126 + revert kaputter setup-token detour aus #22 | **OFFEN — zuerst mergen** |

Pre-flight: PR #23 mergen, lokal pullen, `make up-local-full`, dann Pick aus [`2026-05-25-next-session.md`](./2026-05-25-next-session.md).

## Branch state

Verify mit `git fetch --prune && git log --oneline origin/main -n 8` vor allem.

```
origin/main (Holzi):
  5c3214c fix(oauth): drive claude setup-token under a PTY with bracketed paste (#22)  ← BROKEN, fix in #23
  ac7b57b fix(llm): block activation of pending/expired OAuth credentials (#21)
  df8c59a feat(api/chat): classify upstream errors with status codes in SSE payload (#20)
  1cabf88 feat: per-credential model + GET /credentials/{id}/models (#19)
  1a136e4 docs: SESSION_HANDOFF refresh + next-session plan for 2026-05-24
  d28a5a1 fix(docker): install Node + claude CLI in hermes-server image (#18)
  b81ad40 feat: local dev-stack uses the sqlite credential resolver (#17)
  04f0606 feat: agent loop reads the active DB credential (#16)

origin/main (holzi-frontend):
  0627dee fix(settings): hide Aktivieren on pending/expired OAuth + show hint (#9)
  d9ce5dd feat: per-credential model picker (searchable combobox) (#8)
  b2174ef feat: /settings/llm page (credential list + api-key + claude OAuth) (#7)

Open PRs:
  Holzi#23   fix(oauth): pin claude 2.1.126 + drop the setup-token detour from #22

Closed branches (alle merged oder verworfen):
  Holzi: #19, #20, #21, #22
  holzi-frontend: #8, #9
  haex-claude-proxy: fix/oauth-token-env-extra (verworfen, branch deleted)
  haex-claude-proxy-resolver-sqlite: feat/oauth-token-env-extra (verworfen, branch deleted)
```

## What landed this session (2026-05-24)

1. **#19, #20, #21 gemerged** — per-credential model + chat error semantics + activate-guard für pending OAuth.
2. **#22 gemerged ABER kaputt** — setup-token-PTY-detour. claude-code 2.1.121 hatte den `Paste code here` Prompt entfernt; ich hab statt einfach claude-code zu bumpen versucht `setup-token` + Token-extraction zu nutzen. Die resultierenden `sk-ant-oat01-…` Tokens werden aber von api.anthropic.com mit 429 blockt.
3. **#23 offen** — rebuilds main from #21's tip + revert von #22 + claude 2.1.126 pin (matches Specifyr) + behaltene small fixes (`upstream.py` routet Anthropic IMMER durch Proxy, fixt `/v1/v1/` httpx-collision).
4. **Frontend #8 + #9 gemerged**.
5. **Memory** `[[project-hermes-oauth-claude-2-1]]` aktualisiert: claude-code muss >= 2.1.126 sein, `setup-token` ist NICHT der Weg.
6. **End-to-end manuell verifiziert**: OAuth-Flow + Chat-Antwort von Claude Max läuft lokal.

## What still to do (Reihenfolge)

Per-task in [`2026-05-25-next-session.md`](./2026-05-25-next-session.md). Highlights:

1. **#23 mergen** (Pre-flight)
2. **Frontend Quick-Wins**: Dark-mode toggle, Empty-state, bessere Error-Toasts (alle ~30min)
3. **Conversation auto-title** (Server-side, sichtbar, klein blast-radius)
4. **Mobile Layout** (heute fixed grid → responsive collapse)
5. **E2E `/api/chat` → agent → tool roundtrip test**
6. **Coverage in CI**
7. **External MCP client manager** (der nächste post-MVP Brocken)

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
- **Backend**: `uv run pytest`, `uv run ruff check src tests`, `uv run mypy src`. 258 tests grün auf `fix/pin-claude-2-1-126-and-revert-setup-token` (PR #23).
- **Frontend**: `pnpm test` (30 tests), `pnpm typecheck`, `pnpm run gen:api` (regen nach Backend-Änderung).
- **Local dev-stack**: `make up-local-full` → backend + frontend + proxy + traefik auf `*.localhost:11080`. `.env` muss `HERMES_AUTH_TOKEN` + `HERMES_SECRET_KEY` (64-hex) gesetzt haben.
- **Design docs**: `docs/plans/2026-05-21-hermes-mvp-design.md`, `docs/plans/2026-05-22-roadmap.md`, `docs/plans/2026-05-23-llm-credentials-design.md`.
- **Next-session plan**: [`2026-05-25-next-session.md`](./2026-05-25-next-session.md).
- **Session-start prompt**: [`2026-05-25-prompt.md`](./2026-05-25-prompt.md).
- Conventions: `~/.claude/CLAUDE.md` plus [[feedback-no-vpn]], [[feedback-coderabbit-skip-patterns]], [[feedback-pr-workflow-hermes]], [[project-hermes-oauth-claude-2-1]].
