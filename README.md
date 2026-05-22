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

## Switching LLM providers

Hermes talks to its upstream over the OpenAI `/v1/chat/completions`
contract, so anything that speaks that dialect is a drop-in replacement.
Set `HERMES_LLM_URL` (and `HERMES_LLM_API_KEY` if the provider needs one)
plus `HERMES_MODEL`:

| Provider | `HERMES_LLM_URL` | `HERMES_LLM_API_KEY` | `HERMES_MODEL` |
|---|---|---|---|
| Claude Max via bundled proxy *(default)* | `http://haex-claude-proxy:8080` | *(empty — OAuth via `claude login`)* | `claude-opus-4-7` |
| OpenAI | `https://api.openai.com` | `sk-...` | `gpt-4o` |
| OpenRouter | `https://openrouter.ai/api/v1` | `sk-or-...` | `anthropic/claude-3.5-sonnet` |
| Ollama (local) | `http://ollama:11434/v1` | *(empty)* | `llama3.1` |
| LiteLLM proxy | `http://litellm:4000` | per LiteLLM config | per LiteLLM config |

When switching away from `haex-claude-proxy` you can also drop the
`haex-claude-proxy` service from `docker-compose.yml` (or gate it behind
a Compose profile).

## Linking Signal (one-time)

The Signal worker is disabled unless `HERMES_SIGNAL_NUMBER` is set. To use
Hermes via Signal, link `signal-cli-rest-api` to your account as a secondary
device (your phone stays the primary). Do this once:

```bash
# 1. Generate a linking URI on the container.
make up                      # ensure signal-cli-rest-api is running
docker compose -p hermes exec signal-cli-rest-api \
    signal-cli link -n hermes
# The command prints a tsdevice:/... URI.

# 2. Render that URI as a QR code LOCALLY and scan it from your phone
#    (Signal → Settings → Linked devices → Add device).
#
#    DO NOT paste the tsdevice:/... URI into a public/online QR-code
#    generator — it carries the one-time pairing token and anyone who
#    sees it can hijack the link. Use a local tool:
#        qrencode -t ANSIUTF8 'tsdevice:/?uuid=...&pub_key=...'
#    or any offline QR utility on your machine.

# 3. Set HERMES_SIGNAL_NUMBER to YOUR Signal number in .env (E.164, e.g.
#    +491701234567) and restart:
make down && make up
```

The worker only acts on **Note-to-Self** messages — anything from another
number is dropped. Replies are sent back to Note-to-Self too.

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

Phases 0–4 complete (skeleton, FastAPI + Bearer auth, SQLite/FTS5 memory,
OpenAI-compatible chat-completions proxy, Signal worker with canned reply).
Phase 5 — agent loop with tool-use — is next.
