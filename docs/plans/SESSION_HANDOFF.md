# Session handoff — 2026-05-25

Read this when picking up Hermes ([[project-hermes-agent]]) work in a new conversation. Per-task plan lives in [`2026-05-26-next-session.md`](./2026-05-26-next-session.md); session-start prompt in [`2026-05-26-prompt.md`](./2026-05-26-prompt.md).

## tldr

Hermes ist **production live** auf https://holzi.haex.cloud. Settings-Tabs (LLM + Messenger) sind drin, Signal-Linking-Flow funktioniert UI-driven (kein `docker exec` mehr). Bearer-Token aus dem `/login`-Screen ist die einzige Auth-Schicht.

Keine offenen PRs. `main` ist auf allen drei Repos sauber.

## Was diese Session (2026-05-24 / 2026-05-25) gemacht hat

| PR | Repo | Was | State |
|---|---|---|---|
| Holzi #24 | Holzi | ci: build holzi image multi-arch | merged |
| holzi-frontend #13 + #15 + #16 | holzi-frontend | Dockerise SPA als nginx-static, pnpm-v10-pin, `nuxt generate` statt `nuxt build` | merged |
| haex-claude-proxy-resolver-sqlite #2 + #3 + #5 | proxy-resolver-sqlite | Dockerise plugin → `ghcr.io/haexhub/haex-claude-proxy-resolver-sqlite:latest`, USER-root install layer, `--install-links` damit das Plugin nicht symlinked wird | merged |
| ansible #20 / #21 / #22 / #23 / #24 / #25 | ansible | Holzi-Role + watchtower-fork + holzi-Subdomain-Switch + Authentik-Forward-Auth (verworfen) + holzi-frontend-Role + dedizierter holzi-claude-proxy Sidecar | alle merged |
| Holzi #26 | Holzi | messenger_accounts Schema + Repo + API + Signal-Link-Endpoints + Worker-Hot-Reload | merged |
| holzi-frontend #17 | holzi-frontend | Settings-Tabs Parent-Layout + `/settings/messenger` Page mit QR-Link-State-Machine | merged |

## Production-Topologie (Status)

Drei Container im selben docker-compose Stack auf `~/apps/holzi/` (Backend) + `~/apps/holzi-frontend/`:

```
holzi.haex.cloud
├── /                          → holzi-frontend (nginx static + SPA fallback)
└── /api/* /chat /mcp/* /healthz  → hermes-server (FastAPI)
    └── http://holzi-claude-proxy:8080  (internal, sqlite-resolver, reads /data/hermes.db)
```

Specifyrs `haex-claude-proxy` (Postgres-resolver) bleibt unangetastet auf dem `companies`-Network. Watchtower zieht `:latest` alle 5 min via `nickfedor/watchtower:1.17.1` (containrrr ist archiviert).

Details + Plugin-Bake-Pattern in [[project-holzi-deployment]] (memory).

## Branch state

```
origin/main (Holzi):
  61b95e9 feat(messenger): UI-driven Signal linking + messenger_accounts schema (#26)
  …

origin/main (holzi-frontend):
  <neuester> feat(settings): tabs layout + Signal-linking UI (#17)
  …

origin/main (haex-claude-proxy-resolver-sqlite):
  <neuester> fix(docker): use --install-links so the plugin gets copied (#5)
  …

origin/master (ansible):
  <neuester> feat(holzi): bundle dedicated haex-claude-proxy sidecar (#25)
  …
```

Local leftover branches: `chore/holzi-subdomain`, `chore/watchtower-fork`, `feat/holzi-authentik` etc. in ansible — alles squash-merged, safe zu löschen wenn aufgeräumt werden soll.

**Lokaler Stash auf resolver-sqlite**: `stash@{0}: wip: drop sk-ant-oat01 special-case, route via env-var` — Refactor-Idee, nicht implementiert, kann ignoriert oder später angeschaut werden.

## What still to do

Per-task in [`2026-05-26-next-session.md`](./2026-05-26-next-session.md). Highlights:

1. **Manuelles End-to-End-Smoke** der Signal-Linking-Flow im Browser (QR scannen, aktivieren, Note-to-Self → Antwort) — kann der User selbst, kostet aber Zeit
2. **Phase 3: Telegram-Bot-Integration** (Schema schon da, Routes + Worker fehlen)
3. **Roadmap-Items aus `docs/plans/2026-05-22-roadmap.md` Section A/B** die noch offen sind (Conversation Auto-Title, Mobile Layout, SSE Reconnect, etc.)
4. **Authentication-Cleanup** falls Bearer-only-Modell langfristig bleiben soll: kann die SPA's `/login` etwa Browser-passkeys nutzen? Heute reines Text-Token-Paste

## Workflow reminders

- Active gh-Account: `haexhub`. Push fällt manchmal auf `haex-space`, dann `gh auth switch --user haexhub`. Siehe [[feedback-pr-workflow-hermes]].
- CodeRabbit-Findings triagen gegen [[feedback-coderabbit-skip-patterns]].
- Keine "Generated with Claude Code" Trailer.
- Conversation: Deutsch. Code/commits/PR-Bodies: Englisch.
- Force-Push ist vom auto-classifier geblockt — bei Squash-Merge-Konflikt: PR schließen, fresh Branch von main neu aufmachen.

## Tools / accounts / paths

- **Backend repo**: https://github.com/haexhub/Holzi → `/home/haex/Projekte/Holzi`
- **Frontend repo**: https://github.com/haexhub/holzi-frontend (private) → `/home/haex/Projekte/holzi-frontend`
- **Plugin repo**: https://github.com/haexhub/haex-claude-proxy-resolver-sqlite (private) → `/home/haex/Projekte/haex-claude-proxy-resolver-sqlite`
- **Ansible repo**: https://github.com/haexhub/ansible (private) → `/home/haex/Projekte/ansible` (default branch ist `master`, nicht `main`!)
- **Production-SSH**: `ssh haex@haex.cloud`. App-Verzeichnisse: `~/apps/holzi/`, `~/apps/holzi-frontend/`.
- **Secrets**: `~/Projekte/ansible/secrets/haex.cloud.yml` (gitignored). `secrets.holzi.{auth_token,secret_key}`.
- **Backend**: `uv run pytest`, `uv run ruff check src tests`, `uv run mypy src`. 272 tests.
- **Frontend**: `pnpm test` (38), `pnpm typecheck`.
- **Design docs**: `docs/plans/2026-05-21-hermes-mvp-design.md`, `docs/plans/2026-05-22-roadmap.md`, `docs/plans/2026-05-23-llm-credentials-design.md`.
- **Memory** für neue Sessions: lies [[project-hermes-agent]], [[project-holzi-frontend]], [[project-holzi-deployment]], [[project-haex-claude-proxy-resolver-sqlite]], [[feedback-pr-workflow-hermes]].

## Non-goals / explicit OUTs

- **WhatsApp** — Baileys/whatsapp-web.js verstoßen gegen Meta-ToS, können das persönliche Konto bannen. Nicht implementieren.
- **Authentik-Forward-Auth vor holzi.haex.cloud** — wurde diskutiert + ausgebaut (PR ansible#23). Würde zusätzlich zum HERMES_AUTH_TOKEN-Bearer einen zweiten Login-Layer verlangen den die SPA nicht überbrücken kann. Falls jemals doch nötig: `holzi.use_authentik: true` in Inventory + Blueprint zurückbringen.
- **Alembic** als Migration-Tool — noch nicht nötig, additive Changes laufen über `metadata.create_all` + `_apply_lightweight_migrations`. Erst einführen wenn DROP/RENAME ansteht.
