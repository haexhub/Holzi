# Hermes server — Phase 0 placeholder.
# Phase 1 replaces this with a real multi-stage build (uv sync into a slim
# image, ENTRYPOINT uvicorn).
FROM python:3.12-slim

WORKDIR /app

# Install uv (binary install, no compiler chain needed).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Project metadata + source (deps are empty in Phase 0, so this is fast).
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN uv sync --no-dev

EXPOSE 8082

# Placeholder entrypoint — Phase 1 swaps in `uvicorn hermes.main:app`.
CMD ["uv", "run", "python", "-c", "from hermes import __version__; print(f'hermes {__version__} — Phase 0 placeholder, no app yet')"]
