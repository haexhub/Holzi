# Hermes

Personal AI assistant. The `hermes-server` FastAPI backend powers the
[holzi-frontend](https://github.com/haexhub/holzi-frontend) Web UI.
Memory lives in SQLite + FTS5 (being ported to Postgres + RLS — see
[`docs/plans/2026-06-11-saas-coding-agent-design.md`](docs/plans/2026-06-11-saas-coding-agent-design.md)),
LLM access is OpenAI-compatible (Anthropic OAuth via the bundled
`haex-claude-proxy`, OpenAI, OpenRouter, Google, or any custom
endpoint).

See [`docs/plans/2026-05-21-hermes-mvp-design.md`](docs/plans/2026-05-21-hermes-mvp-design.md)
for the original architecture sketch and the roadmap plans under
[`docs/plans/`](docs/plans/) for what has shipped since.

## Quickstart (local dev)

Prereqs: Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and
**either** Docker + Compose **or** rootless Podman 4.x + `podman
compose`. The Makefile auto-detects whichever is on `$PATH` (Docker
wins ties). Production sandboxing requires rootless Podman — see
[`docs/troubleshooting.md`](docs/troubleshooting.md) for the host
requirements.

```bash
# 1. Install Python deps into a managed venv
make install

# 2. Set up secrets
cp .env.example .env
echo "HERMES_AUTH_TOKEN=$(make -s token)" >> .env
echo "HERMES_SECRET_KEY=$(openssl rand -hex 32)" >> .env
# Optional: edit .env to set HERMES_DOMAIN, HERMES_LOG_FILE, etc.

# 3. Bring up the local dev-stack (backend + LLM proxy + Postgres)
make up-local            # backend only
make up-local-full       # backend + holzi-frontend (Nuxt dev with HMR)
make logs-local
```

The dev-stack routes through a bundled Traefik on `*.localhost` (RFC
6761 → 127.0.0.1, no `/etc/hosts` edits needed):

| URL | Service |
|---|---|
| `http://app.localhost` | Web UI (only with `up-local-full`) |
| `http://app.localhost/api/*` | `hermes-server` REST API (same origin, no CORS) |
| `http://hermes.localhost` | `hermes-server` REST API direct |
| `http://localhost:11000` | Traefik dashboard |

Port 80 already in use? Set `HERMES_LOCAL_WEB_PORT=11080` in `.env` and
the URLs become `http://app.localhost:11080` etc.

On first load the UI shows a login screen — paste the value of
`HERMES_AUTH_TOKEN` from your `.env` (the same `make token` output).
It is persisted in `localStorage` and sent as
`Authorization: Bearer <token>` on every API call.

Then add an LLM credential at `/settings/llm` — see
[`docs/providers.md`](docs/providers.md) for per-provider steps. The
chat-empty-state on `/` points you at the same flow.

## Production deployment

`make up` brings up the stack with an **external** Traefik on the
host (the convention most self-hosters already run). For a greenfield
box that doesn't have Traefik yet, use the bundled one:

```bash
PROXY_NETWORK_EXTERNAL=false make up-traefik
```

You'll need `HERMES_DOMAIN` and `LETSENCRYPT_EMAIL` set in `.env` for
ACME-HTTP-01 to work. Everything else is identical to the dev-stack
configuration.

## Repo layout

```
.
├── docker-compose.yml          # production stack (external Traefik on host)
├── docker-compose.local.yml    # local dev stack (bundled Traefik on *.localhost)
├── docker-compose.local.podman.yml  # Podman-specific overlay (sandbox socket)
├── Dockerfile                  # hermes-server image
├── Dockerfile.sandbox          # workspace sandbox image (`make sandbox-image`)
├── pyproject.toml              # uv-managed Python project
├── Makefile                    # common dev tasks (see `make help`)
├── .env.example                # env-var template — copy to .env
├── src/hermes/                 # FastAPI app
├── tests/                      # pytest suite
└── docs/
    ├── plans/                  # design + per-phase implementation plans
    ├── providers.md            # per-LLM-provider setup
    ├── troubleshooting.md      # diagnoses for the common failure modes
    └── user-guide/             # in-app capability index, loaded by the agent
```

`make help` lists every target. The most-used ones during development:

- `make install` — sync deps into `.venv` via `uv`
- `make dev` — run `hermes-server` directly under `uvicorn --reload` on `localhost:8082` (no containers)
- `make test` / `make lint` / `make typecheck` — pytest / ruff / mypy
- `make up-local` / `make up-local-full` — dev-stack (backend / backend + frontend)
- `make down-local` / `make logs-local` / `make ps-local` — dev-stack lifecycle
- `make sandbox-image` — rebuild the sandbox image after editing `Dockerfile.sandbox`
- `make frontend-reinstall` — recreate the frontend container with a fresh `node_modules` (run after `package.json`/lockfile changes in `../holzi-frontend`)
- `make token` — emit a fresh 32-byte hex token for `HERMES_AUTH_TOKEN`
- `make clean` — `compose down -v`, **destroys `hermes.db`**

## Diagnostics

`GET /api/diagnostics` (bearer-gated) returns a redacted snapshot of
five subsystem checks (database, LLM credential, scheduler, workspace
roots, sandbox runtime). The Web UI surfaces the same data
at `/settings/diagnostics`, with a "Letzte Fehlläufe" panel backed by
`GET /api/runs?status=error`. When anything reports `warning` or
`error`, [`docs/troubleshooting.md`](docs/troubleshooting.md) has the
diagnosis per check.
