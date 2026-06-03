# ---------------------------------------------------------------------------
# Stage 1 — dependency builder
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.7.4 AS uv
FROM python:3.12-slim AS builder

WORKDIR /build

# Install uv from the official image
COPY --from=uv /usr/local/bin/uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock README.md ./

# Install production dependencies
RUN uv sync --production --locked --no-dev

# ---------------------------------------------------------------------------
# Stage 2 — runtime image
# ---------------------------------------------------------------------------
FROM python:3.12-slim

LABEL org.opencontainers.image.title="nornir-napalm-mcp"
LABEL org.opencontainers.image.description="FastMCP server for network device data via NAPALM"

# Create a non-root user
RUN useradd --create-home --shell /bin/bash mcp

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /build/.venv /usr/local

# Copy application code
COPY server.py config.yaml ./

# The inventory directory is mounted at runtime — provide an empty default
# so the image starts without errors if no volume is attached.
RUN mkdir -p inventory && \
    printf '---\n' > inventory/hosts.yaml && \
    printf '---\n' > inventory/groups.yaml && \
    printf '---\nusername: admin\npassword: admin\nport: 22\nplatform: eos\n' > inventory/defaults.yaml

# Allow NORNIR_CONFIG to override the config path at runtime
ENV NORNIR_CONFIG=/app/config.yaml

USER mcp

EXPOSE 8000

# Default: SSE transport so the container is reachable over HTTP.
# Override with --transport stdio for pipe-based clients.
CMD ["python", "server.py", "--transport", "sse", "--host", "0.0.0.0", "--port", "8000"]
