# Nornir-NAPALM FastMCP Server

A FastMCP server that exposes live network device state to AI assistants via NAPALM getters. Nornir handles inventory loading and concurrent device connections over SSH, eAPI, and NETCONF.

All operations are **read-only** — no configuration push is exposed.

---

## Features

| Tool                      | Description                                                                 |
| ------------------------- | --------------------------------------------------------------------------- |
| `nornir_list_inventory`   | List all devices with hostname, platform, and group membership              |
| `nornir_get_facts`        | System facts: vendor, model, OS version, serial number                      |
| `nornir_get_interfaces`   | Interface state and IP address assignments                                  |
| `nornir_run_getter`       | Run any NAPALM getter by name (`arp_table`, `bgp_neighbors`, `vlans`, etc.) |
| `nornir_reload_inventory` | Re-read YAML inventory from disk with add/remove diff                       |

- **Lazy initialization** — server starts even with a broken inventory, exposing the tool catalogue for inspection.
- **Singleton caching** — Nornir instance is initialized once and reused across requests.
- **Pydantic return types** — structured output serialized automatically by FastMCP.
- **SSE and STDIO transport** — run locally for Claude Desktop or expose over HTTP.

---

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Network devices accessible via SSH/eAPI/NETCONF with NAPALM driver support

---

## Setup

```bash
# Clone and install
git clone <repo-url> && cd net-tool

# Install with the drivers you need
uv sync --extra eos         # Arista EOS
uv sync --extra junos       # Juniper JunOS
uv sync --extra ios         # Cisco IOS/IOS-XE
uv sync --extra all-drivers # All of the above
```

### Configure inventory

Edit the YAML files under `inventory/`:

- `hosts.yaml` — per-device entries (hostname, platform, groups)
- `groups.yaml` — group-level overrides
- `defaults.yaml` — global defaults (credentials, port)

> **Security:** Do not commit plaintext credentials. Use environment variables via `nornir-env-transform`, HashiCorp Vault, or a secrets manager.

### Environment variables

| Variable        | Default       | Description                         |
| --------------- | ------------- | ----------------------------------- |
| `NORNIR_CONFIG` | `config.yaml` | Path to the Nornir bootstrap config |

---

## Usage

### Local development (MCP Inspector)

```bash
fastmcp dev server.py
```

### Claude Desktop

```bash
fastmcp install server.py
```

### CLI

```bash
# STDIO transport (default, for Claude Desktop)
python server.py --transport stdio

# SSE transport (for network-accessible deployments)
python server.py --transport sse --host 0.0.0.0 --port 8000
```

### Docker

```bash
# Build
docker build -t nornir-napalm-mcp .

# Run — mount your inventory directory
docker run -p 8000:8000 \
  -v $(pwd)/inventory:/app/inventory \
  nornir-napalm-mcp
```

#### docker-compose

```yaml
services:
  mcp:
    image: nornir-napalm-mcp
    ports:
      - "8000:8000"
    volumes:
      - ./inventory:/app/inventory:ro
    environment:
      - NORNIR_CONFIG=/app/config.yaml
    restart: unless-stopped
```

### NAPALM getters

Use `nornir_run_getter` with any of these:

`arp_table`, `bgp_neighbors`, `bgp_neighbors_detail`, `bgp_config`,
`environment`, `lldp_neighbors`, `lldp_neighbors_detail`,
`mac_address_table`, `ntp_servers`, `ntp_stats`,
`optics`, `route_to`, `snmp_information`, `users`, `vlans`

---

## Project Structure

```
net-tool/
├── server.py              # FastMCP server, Nornir init, tool definitions
├── config.yaml            # Nornir bootstrap config (inventory + runner)
├── pyproject.toml         # Build config, dependencies, tool settings
├── Dockerfile             # Multi-stage build (uv → slim runtime)
├── inventory/
│   ├── hosts.yaml         # Per-device entries
│   ├── groups.yaml        # Group-level overrides
│   └── defaults.yaml      # Global defaults
└── tests/
    ├── conftest.py        # Fake Nornir stubs and fixtures
    └── test_helpers.py    # Unit tests for server.py
```

---

## Development

```bash
# Run tests
uv run pytest

# Lint
uv run ruff check .

# Type check (strict mode)
uv run mypy server.py

# Build wheel
uv build
```

---

## Post-MVP Roadmap

- [ ] Safe config push (`napalm_configure` with mandatory dry-run)
- [ ] Group-based bulk queries ("get ARP for all spines")
- [ ] NetBox inventory backend (`nornir-netbox`)
- [ ] Connection pooling / persistent NAPALM connections
- [ ] Structured error codes surfaced back to the LLM
