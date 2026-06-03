# Troubleshooting

Diagnoses for the failure modes the
[`/api/diagnostics`](../src/hermes/routes/diagnostics.py) endpoint
(surfaced at `/settings/diagnostics` in the Web UI) is most likely to
flag, plus the few that happen *before* you can reach the UI at all.
Each entry: symptom → root cause → fix.

## Backend won't start (`HERMES_AUTH_TOKEN` missing)

**Symptom.** `make up-local` boots all containers but `hermes-server`
exits immediately. `make logs-local` shows a Pydantic validation error
mentioning `auth_token`.

**Cause.** `HERMES_AUTH_TOKEN` is mandatory (`min_length=1`,
[`src/hermes/config.py`](../src/hermes/config.py)). An empty value
fails validation at boot.

**Fix.**

```bash
echo "HERMES_AUTH_TOKEN=$(make -s token)" >> .env
make down-local && make up-local
```

The same token gets pasted into the Web UI login screen; it is
persisted in `localStorage` under `hermes.auth.token` and sent as
`Authorization: Bearer <token>` on every API call.

## Web UI shows "unauthorized" on every request

**Symptom.** UI loads, login form accepted, but every page or API
panel shows a 401-style error.

**Cause.** The bearer token in `localStorage` doesn't match
`HERMES_AUTH_TOKEN`. Common reasons:

- You rotated `HERMES_AUTH_TOKEN` in `.env` but the browser still
  holds the old one.
- You pasted the value with a trailing newline. (The frontend trims
  on `setToken`, but a hand-edited `localStorage` entry might still
  have whitespace.)
- You're talking to a different `hermes-server` instance than you
  think (Traefik routing to the wrong container, two stacks with
  conflicting `.env`).

**Fix.** Browser DevTools → Application → Local Storage →
`http://app.localhost` → delete `hermes.auth.token`, reload, paste
again. If that doesn't fix it, confirm with
`grep HERMES_AUTH_TOKEN .env` and
`make ps-local | grep hermes-server`.

## Frontend can't reach backend (no `/api/*` response)

**Symptom.** Web UI loads at `http://app.localhost` but the network
panel shows every `/api/*` call returning 502 / connection refused /
CORS preflight failure.

**Causes & fixes.**

- **Traefik isn't routing.** `make ps-local` — both `traefik` and
  `hermes-server` must be Up. Traefik dashboard at
  `http://localhost:11000` shows the live router table; if there's no
  router for `app.localhost`, restart with `make down-local &&
  make up-local-full`.
- **Port 80 already taken on the host.** Set
  `HERMES_LOCAL_WEB_PORT=11080` in `.env` and restart. URLs become
  `http://app.localhost:11080`.
- **CORS errors.** There is no CORS middleware — the dev-stack serves
  UI and API from the same origin via Traefik. If you see a CORS
  preflight failure, you're hitting the backend directly from a
  different origin (e.g. `vite dev` against `http://hermes.localhost`).
  Either run the frontend through Traefik (`make up-local-full`) or
  switch to a same-origin reverse proxy.

## `/settings/diagnostics` → LLM warning ("no active credential")

**Symptom.** `/settings/diagnostics` shows LLM as `warning` with
"no active LLM credential — chat will fail until one is configured".
Sending a chat message returns `503`.

**Cause.** Either no row in `llm_credentials`, or the active row is
in an unauthorised OAuth state.

**Fix.** Open `/settings/llm`, add a credential and activate it. See
[`providers.md`](providers.md) for per-provider steps. For
`oauth_claude` rows: a row in `oauth_status='pending'` /
`'expired'` cannot be activated — re-run the OAuth flow (it sweeps
the stale row).

## LLM credential gives 401 from upstream

**Symptom.** Credential is active, diagnostics shows LLM `ok`, but
the model dropdown on `/settings/llm` reports 502 and chat returns
`503`. `make logs-local` mentions a 401 from the provider.

**Causes & fixes.**

- **Bad API key.** Re-issue at the provider console and re-add the
  credential. Hermes doesn't expose the plaintext; recovery is "delete
  + add" not "edit".
- **`HERMES_SECRET_KEY` drift.** The credential was encrypted with one
  AES key but `.env` now has a different one. Decryption fails →
  agent treats the credential as unusable. Either restore the
  original `HERMES_SECRET_KEY` or `DELETE` the credential and add it
  again.
- **OAuth token expired (`oauth_claude`).** Open `/settings/llm`,
  re-start the OAuth flow on the row. The bundled
  `haex-claude-proxy` refreshes automatically; a stuck row points at
  a `claude` CLI crash inside the proxy container — `docker compose
  logs haex-claude-proxy` will show it.

## `/settings/diagnostics` → Sandbox warning ("not configured")

**Symptom.** Diagnostics shows Sandbox runtime as `warning` with
"HERMES_SANDBOX_SOCKET not set — tool calls that need a sandbox will
fail". Workspace and exec endpoints return `503`.

**Cause.** Production sandboxing requires rootless **Podman** (not
Docker). On a Docker-only host the Makefile boots `hermes-server`
without the Podman control socket, the sandbox manager resolves to
`None`, and any tool that needs a sandbox fails loudly. Chat, memory,
tasks, Signal, and the frontend work normally.

**Fix (if you want sandbox tools).**

1. Install rootless Podman 4.x.
2. Make sure `subuid` / `subgid` are populated for your user
   (`grep $(whoami) /etc/subuid /etc/subgid` — non-empty rows).
3. Delegate the cpu, cpuset, memory, and pids cgroup controllers for
   the user systemd slice. Without this, container creation fails
   with "cgroup not delegated".
4. For disk-quota enforcement (`HERMES_SANDBOX_DISK_QUOTA=true`), the
   container store must be on XFS with pquota — ext4/btrfs reject the
   `StorageOpt size` overlay and creation fails. The default keeps
   the quota off, so the agent runs on any filesystem.
5. Run the dev-stack with `make CONTAINER_BIN=podman up-local-full`.
   The Makefile auto-layers `docker-compose.local.podman.yml`, which
   wires the rootless Podman socket into `hermes-server`.

## `/settings/diagnostics` → Sandbox error ("manager failed to start")

**Symptom.** Diagnostics shows Sandbox runtime as `error` — the socket
path is configured but the manager didn't initialise.

**Cause.** The path in `HERMES_SANDBOX_SOCKET` (or the auto-detected
Podman socket) isn't reachable from inside `hermes-server`. Usually
either:

- The host's Podman socket isn't running
  (`systemctl --user start podman.socket`).
- `XDG_RUNTIME_DIR` was empty when the Makefile resolved the default,
  producing a bogus `/podman/podman.sock` path. The Makefile falls
  back to `/run/user/$(id -u)/podman/podman.sock` in that case —
  verify with `make -np | grep HERMES_CONTAINER_SOCKET`.
- The mount path differs between the host and the container. Set
  `HERMES_CONTAINER_SOCKET=/run/user/1000/podman/podman.sock`
  (matching `id -u`) explicitly in `.env`.

Also see the **"Persistent sandbox-crash log"** under
`/settings/diagnostics` (Plan 20-A) — entries in
`sandbox_crashes` show the exact Podman error that took the runtime
down.

## Workspace browser empty / `/settings/workspaces` shows nothing

**Symptom.** `/settings/workspaces` lists no workspaces; `/files`
(workspace browser tab in the chat side rail) is empty. Diagnostics
shows Workspaces as `warning` with "no workspaces configured".

**Cause.** No row in the `workspaces` table. Plan 25-A moved the
source of truth from `HERMES_WORKSPACE_ROOTS` (env-var, boot-time
backfill only) to the DB.

**Fix.** `/settings/workspaces` → **Add workspace** → pick a slug and
a host path. The first read spins up the sandbox lazily — make sure
sandbox is `ok` first (see above), otherwise the workspace row exists
but file reads return `503`.

## MCP server crashed (`/settings/skills`)

**Symptom.** `/settings/skills` shows an MCP server row with a red
status. Tool calls that route to that server fail. The `mcp_status`
agent meta-tool reports the same.

**Cause.** A spawned external MCP server (Node/Python subprocess
under `HERMES_EXTERNAL_MCP` or DB-managed `mcp_servers`) exited
non-zero, hung on stdin, or failed health checks.

**Fix.** `/settings/skills` → **Restart** on the affected row. The
restart action doesn't require approval (Plan 32-A's design — restart
is reversible). If restart loops, click **Logs** to see the
subprocess's stderr tail; common culprits are a wrong working
directory in the server definition or a missing API token in its
environment.

## Signal Note-to-Self silent (linked secondary device)

**Symptom.** Signal is linked, the worker is running, messages send
fine to other numbers — but Note-to-Self messages never reach Hermes.

**Cause.** `signal-cli` linked as a **secondary device** filters out
`syncMessage` envelopes regardless of `MODE` — verified upstream
limit. Note-to-Self is a sync message, so the worker never sees it.

**Fix.** Connect Signal as a **primary device** for Hermes:

- Either use a dedicated phone number (recommended — keeps your
  personal Signal account untouched), or
- Migrate your existing number to Hermes (the Signal app on the phone
  then needs re-pairing).

See `docs/user-guide/messengers.md` for the messenger model overview.

## `/api/logs` returns 503

**Symptom.** `/settings/logs` (the Logs page) shows a 503 banner.

**Cause.** `HERMES_LOG_FILE` is unset, so structlog only writes to
stdout and there's no file to tail.

**Fix.** Set `HERMES_LOG_FILE=/var/log/hermes/agent.log` (or any
path the `hermes-server` container can write to) in `.env` and
restart. The rotating file handler caps at
`HERMES_LOG_FILE_MAX_BYTES` (default 10 MiB) × `HERMES_LOG_FILE_BACKUP_COUNT`
(default 3) → ~40 MiB ceiling.

## Frontend container has stale `node_modules`

**Symptom.** `make up-local-full` boots but the frontend container
crashes with "module not found" or "vite missing dependency" after
you pulled fresh frontend code with new packages.

**Cause.** The frontend uses an anonymous Docker volume for
`node_modules` so host installs don't fight container installs. After
a `package.json` / lockfile change, that anonymous volume still holds
the old dep tree.

**Fix.**

```bash
make frontend-reinstall
```

That recreates the `holzi-frontend` container with
`--renew-anon-volumes`, forcing a fresh `pnpm install`.

## `make clean` destroyed the database

**Symptom.** All conversations, credentials, workspaces, tasks gone
after running `make clean`.

**Cause.** `make clean` runs `compose down -v`, which removes the
`hermes-data` named volume that holds `hermes.db`. This is documented
in the Makefile help (`# DESTROYS hermes.db`) but easy to miss.

**Fix.** None retrospectively. Restore from a backup of the
`hermes-data` volume contents. To avoid recurrence, take a snapshot
before structural experiments:

```bash
docker run --rm -v hermes_hermes-data:/data -v "$PWD":/backup \
    alpine tar czf /backup/hermes-data.tgz -C /data .
```

(Substitute `podman` for `docker` on Podman hosts.)
