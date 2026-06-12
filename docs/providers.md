# LLM provider setup

Hermes talks to any upstream that speaks the OpenAI
`/v1/chat/completions` contract. Credentials are stored in the
`llm_credentials` Postgres table (AES-256-GCM-encrypted at rest) and
managed through `/settings/llm` in the Web UI. Exactly one credential
is active at a time — the agent loop and the bundled
`haex-claude-proxy` both read it via the same encrypted column.

## TL;DR

Most users:

1. Open `/settings/llm` in the Web UI.
2. Click **Add credential**, pick the provider, paste the API key
   (or run the OAuth flow for Anthropic Max).
3. Click **Activate** on the row you want to use.
4. Optionally pick a specific model via the **Model** dropdown — the
   list is populated from the provider's `/v1/models` endpoint.

The fallback `HERMES_LLM_URL` / `HERMES_LLM_API_KEY` / `HERMES_MODEL`
env vars (see `.env.example`) are only used when **no DB credential is
active** — useful for bootstrapping headless deployments.

## Supported providers

The `provider` column accepts five literal values, enforced server-side
in `src/hermes/routes/llm.py`. Anything else is rejected with `422`.

| `provider` | Auth mode(s) | `/v1/models` lister | Notes |
|---|---|---|---|
| `anthropic` | `api_key` **or** `oauth_claude` | Yes (api_key) / curated fallback (OAuth) | Claude Max via OAuth requires the bundled `haex-claude-proxy` |
| `openai` | `api_key` | Yes | Default base `https://api.openai.com` |
| `openrouter` | `api_key` | Yes | Default base `https://openrouter.ai/api` |
| `google` | `api_key` | Yes | Gemini via `generativelanguage.googleapis.com` |
| `custom` | `api_key` | **No** — set the model manually | Any other OpenAI-compatible endpoint (LiteLLM, Ollama, …); `base_url` mandatory |

### Anthropic — API key

1. Get a key from <https://console.anthropic.com/>.
2. `/settings/llm` → **Add credential** → provider `anthropic` →
   paste the `sk-ant-…` key, optionally override the base URL.
3. Activate. The model dropdown is populated from
   `https://api.anthropic.com/v1/models`.

### Anthropic — Claude Max via OAuth (bundled proxy)

This is the default in `.env.example`. It uses the `haex-claude-proxy`
container, which runs the `claude` CLI in OAuth mode and hands the
freshly-minted bearer token to Hermes at request time. The OAuth flow
runs from inside Hermes — no shell access into the proxy container
required.

1. Make sure the `haex-claude-proxy` image is built (`make build` in
   the `haex-claude-proxy` repo) and that the sister-directory
   `haex-claude-proxy-resolver-sqlite` exists with `node_modules`
   installed. See the comment block at the top of
   `docker-compose.local.yml` for the exact layout the dev-stack
   expects.
2. `HERMES_SECRET_KEY` must be set in `.env` to a 64-hex AES key, and
   must be **identical** to whatever the resolver reads — a mismatch
   turns every chat into a `500`. Generate once via
   `openssl rand -hex 32`.
3. `/settings/llm` → **Add credential** → provider `anthropic`,
   mode `oauth_claude`. The UI starts the subprocess flow, shows you
   a verification URL, asks for the code Anthropic returns, and
   persists the resulting OAuth credentials file as ciphertext.
4. Activate. The model dropdown falls back to the curated list (Opus
   4.7, Sonnet 4.6, Haiku 4.5) because Anthropic's `/v1/models`
   requires an API key, not an OAuth token.

Only one `oauth_claude` credential exists at any time — starting a
fresh flow tears down the previous row (matches the single-Anthropic-
identity-per-instance assumption).

### OpenAI

1. Get a key from <https://platform.openai.com/api-keys>.
2. `/settings/llm` → **Add credential** → provider `openai` →
   paste the `sk-…` key.
3. Activate. The model dropdown calls `https://api.openai.com/v1/models`.

### OpenRouter

1. Get a key from <https://openrouter.ai/keys>.
2. `/settings/llm` → **Add credential** → provider `openrouter` →
   paste the `sk-or-…` key.
3. Activate. The model dropdown calls
   `https://openrouter.ai/api/v1/models` — OpenRouter exposes every
   model from every connected provider, so the list is long.

### Google (Gemini)

1. Get a key from <https://aistudio.google.com/app/apikey>.
2. `/settings/llm` → **Add credential** → provider `google` →
   paste the key.
3. Activate. The model dropdown calls
   `https://generativelanguage.googleapis.com/v1beta/models`.

### Custom (LiteLLM, Ollama, other OpenAI-compatible proxies)

Use this when none of the above fit — a self-hosted LiteLLM proxy,
a local Ollama, a private gateway, etc.

1. Run the upstream somewhere Hermes can reach (e.g. `ollama serve`
   on the host, or a LiteLLM service in the same Compose network).
2. `/settings/llm` → **Add credential** → provider `custom`, set
   `base_url` to the OpenAI-compatible root (e.g.
   `http://host.docker.internal:11434/v1` for host-side Ollama from
   the container, or `http://litellm:4000` for a sibling service).
3. Paste a placeholder string into `api_key` if the upstream doesn't
   need one (it must be non-empty — Hermes always sends an
   `Authorization: Bearer …` header).
4. Activate. There is no `/v1/models` lister for `custom` (Hermes
   can't know which dialect you've configured), so set the model
   string by hand using **Edit model** on the row.

## Env-var fallback (no DB credential active)

When the `llm_credentials` table has no active row, the agent loop
falls back to:

```bash
HERMES_LLM_URL=http://haex-claude-proxy:8080  # OpenAI-compatible URL
HERMES_LLM_API_KEY=                            # optional Bearer
HERMES_MODEL=claude-opus-4-7                   # model id passed through
```

Useful for bootstrapping a fresh deployment before opening
`/settings/llm` — but the UI flow is the supported path for everything
beyond first boot.

## Diagnostics

`/settings/diagnostics` shows whether the active LLM credential is
present (`ok` / `warning`). It never echoes API key plaintext,
ciphertext, or the master key — only `provider`, `display_name`,
and `model`. If chat suddenly returns `503`, check:

- the credential is still `is_active=1`,
- `HERMES_SECRET_KEY` hasn't drifted from what the credential was
  encrypted with,
- for `oauth_claude`: the OAuth token hasn't expired (the proxy
  refreshes automatically; a stuck flow leaves the row in
  `oauth_status='expired'`).

[`docs/troubleshooting.md`](troubleshooting.md) has the recovery steps.
