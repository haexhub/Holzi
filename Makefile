.DEFAULT_GOAL := help
SHELL := /bin/bash

# Pin all Compose invocations to the project so naming stays predictable
# even when CWD differs.
COMPOSE := docker compose -p hermes

.PHONY: help install dev lint typecheck test up up-traefik down logs ps clean token

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

down: ## docker compose down
	$(COMPOSE) down

logs: ## Tail all service logs
	$(COMPOSE) logs -f --tail=200

ps: ## Show running services
	$(COMPOSE) ps

clean: ## Remove containers + volumes (DESTROYS hermes.db)
	$(COMPOSE) down -v

token: ## Print a fresh 32-byte hex token suitable for HERMES_AUTH_TOKEN
	@openssl rand -hex 32
