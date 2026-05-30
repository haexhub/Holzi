.DEFAULT_GOAL := help
SHELL := /bin/bash

# Pin all Compose invocations to the project so naming stays predictable
# even when CWD differs.
#
# Container runtime (Plan 20-B): auto-detect docker first, then podman. The
# preference for docker is intentional — most contributors install docker
# first; production sandboxing still requires rootless Podman (Plan 11b-a)
# and the Podman path layers in docker-compose.local.podman.yml so the agent
# can spawn workspace sandboxes. Docker-only hosts boot sandbox-less; the
# agent reports sandbox=warning on /api/diagnostics and workspace + exec
# endpoints return 503.
#
# Explicit override always wins:
#   make CONTAINER_BIN=podman up-local-full
#   make COMPOSE_BIN="docker compose" up-local-full
CONTAINER_BIN ?= $(shell command -v docker >/dev/null 2>&1 && echo docker || (command -v podman >/dev/null 2>&1 && echo podman))
COMPOSE_BIN ?= $(CONTAINER_BIN) compose
COMPOSE := $(COMPOSE_BIN) -p hermes

# Host path of the container-control socket. Mounted into traefik for label
# discovery on both runtimes, and (only under Podman, via the overlay) into
# hermes-server for sandbox spawning.
ifeq ($(CONTAINER_BIN),docker)
  export HERMES_CONTAINER_SOCKET ?= /var/run/docker.sock
  COMPOSE_LOCAL_OVERLAYS :=
  SANDBOX_IMAGE_DEP :=
else ifeq ($(CONTAINER_BIN),podman)
  # XDG_RUNTIME_DIR is normally /run/user/$(id -u) for an interactive
  # session but can be empty under sudo / cron / minimal CI shells.
  # Fall back to the canonical runtime dir derived from the current uid
  # so the resolved socket path never starts with "/podman/...".
  PODMAN_SOCKET_DEFAULT := $(if $(XDG_RUNTIME_DIR),$(XDG_RUNTIME_DIR),/run/user/$(shell id -u))/podman/podman.sock
  export HERMES_CONTAINER_SOCKET ?= $(PODMAN_SOCKET_DEFAULT)
  COMPOSE_LOCAL_OVERLAYS := -f docker-compose.local.podman.yml
  SANDBOX_IMAGE_DEP := sandbox-image
endif

COMPOSE_LOCAL := $(COMPOSE_BIN) -p hermes-local -f docker-compose.local.yml $(COMPOSE_LOCAL_OVERLAYS)

# Built outside compose — sandboxes are spawned dynamically by the agent, not
# declared as a service.
SANDBOX_IMAGE ?= hermes-sandbox:dev

.PHONY: help install dev lint typecheck test _check-runtime up up-traefik up-local up-local-full sandbox-image frontend-reinstall down down-local logs logs-local ps ps-local clean token

_check-runtime:
	@if [ -z "$(CONTAINER_BIN)" ]; then \
	  echo "Error: no container runtime detected."; \
	  echo "Install docker or podman, or invoke with CONTAINER_BIN=... COMPOSE_BIN=..."; \
	  exit 1; \
	fi

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} \
	     /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install: ## Sync dependencies via uv (creates .venv)
	uv sync --extra dev

dev: ## Run hermes-server locally (Phase 1+)
	uv run uvicorn hermes.main:app --reload --host 0.0.0.0 --port 8082

lint: ## Ruff lint
	uv run ruff check src tests

typecheck: ## Mypy strict
	uv run mypy src

test: ## Pytest
	uv run pytest

up: _check-runtime ## docker compose up (external Traefik on host)
	$(COMPOSE) up -d

up-traefik: _check-runtime ## docker compose up with bundled Traefik (greenfield boxes)
	$(COMPOSE) --profile traefik up -d

up-local: _check-runtime $(SANDBOX_IMAGE_DEP) ## Local dev-stack on *.localhost (backend only, no frontend)
	$(COMPOSE_LOCAL) up -d --build

up-local-full: _check-runtime $(SANDBOX_IMAGE_DEP) ## Local dev-stack + holzi-frontend (Nuxt dev with HMR)
	$(COMPOSE_LOCAL) --profile frontend up -d --build

sandbox-image: _check-runtime ## Build the sandbox runtime image used for workspace/ephemeral sandboxes
	$(CONTAINER_BIN) build -t $(SANDBOX_IMAGE) -f Dockerfile.sandbox .

frontend-reinstall: _check-runtime ## Recreate holzi-frontend with fresh node_modules (run after package.json/lockfile changes)
	$(COMPOSE_LOCAL) --profile frontend up -d --force-recreate --renew-anon-volumes holzi-frontend

down: _check-runtime ## docker compose down
	$(COMPOSE) down

down-local: _check-runtime ## docker compose down for the local dev-stack
	$(COMPOSE_LOCAL) --profile frontend down

logs: _check-runtime ## Tail all service logs
	$(COMPOSE) logs -f --tail=200

logs-local: _check-runtime ## Tail local dev-stack logs
	$(COMPOSE_LOCAL) logs -f --tail=200

ps: _check-runtime ## Show running services
	$(COMPOSE) ps

ps-local: _check-runtime ## Show running services in the local dev-stack
	$(COMPOSE_LOCAL) ps

clean: _check-runtime ## Remove containers + volumes (DESTROYS hermes.db)
	$(COMPOSE) down -v

token: ## Print a fresh 32-byte hex token suitable for HERMES_AUTH_TOKEN
	@openssl rand -hex 32
