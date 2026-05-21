FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Resolve and install dependencies first for cache-friendly rebuilds.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and install the project itself.
COPY src/ ./src/
RUN uv sync --frozen --no-dev

EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8082/healthz', timeout=2).status == 200 else 1)" || exit 1

CMD ["uv", "run", "uvicorn", "hermes.main:app", "--host", "0.0.0.0", "--port", "8082"]
