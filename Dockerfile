# ---------------------------------------------------------------------------
# Stage 1 — dependency builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools
RUN pip install --upgrade pip hatchling

# Copy dependency files first for layer caching
COPY pyproject.toml .
COPY README.md .

# Install runtime deps into a prefix we can copy cleanly
# Install base package first
RUN pip install --prefix=/install .
# Then install driver dependencies individually if the combined extra fails
RUN pip install --prefix=/install napalm[eos] || echo "Warning: napalm[eos] extra not available, continuing" && \
    pip install --prefix=/install napalm[junos] || echo "Warning: napalm[junos] extra not available, continuing" && \
    pip install --prefix=/install napalm[ios] || echo "Warning: napalm[ios] extra not available, continuing"

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
COPY --from=builder /install /usr/local

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
