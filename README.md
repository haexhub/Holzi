# Hermes

Personal AI assistant. Runs on a VPS, reachable via Signal (Note-to-Self),
a web UI, and from inside VSCode through Cline/Roo Code. Uses a Claude Max
subscription via the [`haex-claude-proxy`](https://github.com/) and keeps
memory across all channels in SQLite + FTS5.

See [`docs/plans/2026-05-21-hermes-mvp-design.md`](docs/plans/2026-05-21-hermes-mvp-design.md)
for the full architecture and the 10-phase roadmap.

## Quickstart (local dev)

Prereqs: Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Docker + Compose.

```bash
# 1. Install Python deps into a managed venv
make install

# 2. Set up secrets
cp .env.example .env
echo "HERMES_AUTH_TOKEN=$(make -s token)" >> .env
# Edit .env: set HERMES_DOMAIN, LETSENCRYPT_EMAIL, HERMES_SIGNAL_NUMBER

# 3. Bring up the stack (uses external Traefik on the host by default)
make up
make logs
```

For a box with no Traefik yet, use the bundled one:

```bash
PROXY_NETWORK_EXTERNAL=false make up-traefik
```

## Repo layout

```
.
├── docker-compose.yml     # hermes-server + signal-cli-rest-api + haex-claude-proxy
│                          #   (+ traefik under `--profile traefik`)
├── pyproject.toml         # uv-managed Python project
├── Makefile               # common dev tasks (install, dev, test, up, down, logs)
├── .env.example           # env-var template — copy to .env
├── src/hermes/            # FastAPI app (filled in Phase 1+)
├── tests/                 # pytest suite
└── docs/plans/            # design docs and per-phase implementation plans
```

## Status

Phase 0 (project skeleton) complete. Phase 1 (hermes-server skeleton with
`/healthz` + Bearer-token middleware) is next.
