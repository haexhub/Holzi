FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Node + claude-code are required at runtime: the OAuth subprocess driver
# spawns `claude auth login --claudeai` to obtain the Anthropic refresh
# token before AES-encrypting it into llm_credentials. Pinned to 2.1.126
# to match the haex-claude-proxy image — 2.1.121 silently dropped the
# `Paste code here if prompted >` stdin prompt, making the OAuth driver
# hang on a TTY paste it can't produce. 2.1.126 still has the simple
# pipe-friendly flow Specifyr's driver was written against.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code@2.1.126 \
 && apt-get purge -y curl gnupg \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# Resolve and install dependencies first for cache-friendly rebuilds.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and install the project itself.
COPY src/ ./src/
# User-guide markdown is read at runtime by hermes.capabilities (injected
# into the system prompt + served by the read_user_guide tool). Resolved
# relative to the package via `Path(__file__).parents[2]`, so the docs
# need to sit at /app/docs/ next to /app/src/.
COPY docs/user-guide/ ./docs/user-guide/
RUN uv sync --frozen --no-dev

EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8082/healthz', timeout=2).status == 200 else 1)" || exit 1

CMD ["uv", "run", "uvicorn", "hermes.main:app", "--host", "0.0.0.0", "--port", "8082"]
