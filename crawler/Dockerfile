FROM python:3.13-slim

WORKDIR /app

# System deps for asyncpg build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency manifests
COPY pyproject.toml uv.lock ./

# Install deps (no package yet — src not copied)
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code, then install the package
COPY src/ src/
RUN uv sync --frozen --no-dev

# Run as non-root
RUN useradd --create-home --shell /bin/bash crawler
USER crawler

CMD ["uv", "run", "python", "-m", "src.main"]
