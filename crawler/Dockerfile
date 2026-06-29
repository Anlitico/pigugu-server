FROM python:3.13-slim

WORKDIR /app

# System deps for asyncpg build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy everything needed for sync
COPY pyproject.toml uv.lock ./
COPY src/ src/

# Install deps + project in one step
RUN uv sync --frozen --no-dev

# Run as non-root
RUN useradd --create-home --shell /bin/bash crawler
USER crawler

CMD ["uv", "run", "--no-sync", "python", "-m", "src.main"]
