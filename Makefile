.DEFAULT_GOAL := help
SHELL := /bin/bash

# Pin all Compose invocations to the project so naming stays predictable
# even when CWD differs.
COMPOSE := docker compose -p hermes

COMPOSE_LOCAL := docker compose -p hermes-local -f docker-compose.local.yml

.PHONY: help install dev lint typecheck test up up-traefik up-local up-local-full down down-local logs logs-local ps ps-local clean token

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

up: ## docker compose up (external Traefik on host)
	$(COMPOSE) up -d

up-traefik: ## docker compose up with bundled Traefik (greenfield boxes)
	$(COMPOSE) --profile traefik up -d

up-local: ## Local dev-stack on *.localhost (backend only, no frontend)
	$(COMPOSE_LOCAL) up -d --build

up-local-full: ## Local dev-stack + holzi-frontend (Nuxt dev with HMR)
	$(COMPOSE_LOCAL) --profile frontend up -d --build

down: ## docker compose down
	$(COMPOSE) down

down-local: ## docker compose down for the local dev-stack
	$(COMPOSE_LOCAL) --profile frontend down

logs: ## Tail all service logs
	$(COMPOSE) logs -f --tail=200

logs-local: ## Tail local dev-stack logs
	$(COMPOSE_LOCAL) logs -f --tail=200

ps: ## Show running services
	$(COMPOSE) ps

ps-local: ## Show running services in the local dev-stack
	$(COMPOSE_LOCAL) ps

clean: ## Remove containers + volumes (DESTROYS hermes.db)
	$(COMPOSE) down -v

token: ## Print a fresh 32-byte hex token suitable for HERMES_AUTH_TOKEN
	@openssl rand -hex 32
