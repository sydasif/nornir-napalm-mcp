# Nornir-NAPALM FastMCP Server

A FastMCP server that exposes live network device state to AI assistants via NAPALM getters. Nornir handles inventory loading and concurrent device connections over SSH, eAPI, and NETCONF.

All operations are **read-only** — no configuration push is exposed.

---

## Features

| Tool                      | Description                                                                 |
| ------------------------- | --------------------------------------------------------------------------- |
| `nornir_list_inventory`   | List all devices with hostname, platform, and group membership              |
| `nornir_get_facts`        | System facts: vendor, model, OS version, serial number                      |
| `nornir_run_getter`       | Run any NAPALM getter by name (`arp_table`, `bgp_neighbors`, `vlans`, etc.) |
| `nornir_get_config`       | Retrieve running and/or startup configuration from a device                 |
| `nornir_run_cli`          | Execute read-only `show` commands via NAPALM's CLI method                   |
| `nornir_list_getters`     | Introspect available NAPALM getters for each platform in the inventory      |
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

# Install
uv sync
```

### Nornir configuration

The server requires a Nornir configuration file. You can provide it in two ways:

#### 1. Default (no env var)

Place a `config.yaml` in the project root:

```yaml
---
inventory:
  plugin: SimpleInventory
  options:
    host_file: "inventory/hosts.yaml"
    group_file: "inventory/groups.yaml"
    defaults_file: "inventory/defaults.yaml"

runner:
  plugin: threaded
  options:
    num_workers: 10

logging:
  enabled: false
```

_Note: The inventory files must exist relative to this config file._

#### 2. External (using `NORNIR_CONFIG`)

Set the `NORNIR_CONFIG` environment variable to point to an external configuration file (e.g., for use with a test lab):

```bash
export NORNIR_CONFIG=/path/to/your/config.yaml
```

---

### Environment variables

| Variable        | Default       | Description                         |
| --------------- | ------------- | ----------------------------------- |
| `NORNIR_CONFIG` | `config.yaml` | Path to the Nornir bootstrap config |

### MCP client configuration

Register this server with any MCP client (Claude Desktop, VS Code, etc.) by adding one of the following to your project's `.mcp.json`:

#### 1. Default (Local config)

Uses `config.yaml` in the `net-tool` directory.

```json
{
  "mcpServers": {
    "nornir": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/net-tool", "python", "server.py"]
    }
  }
}
```

#### 2. External (Lab config)

Uses an external configuration file via environment variable.

```json
{
  "mcpServers": {
    "nornir": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/net-tool",
        "python",
        "server.py"
      ],
      "env": {
        "NORNIR_CONFIG": "/path/to/lab/config.yaml"
      }
    }
  }
}
```

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

### NAPALM getters

Use `nornir_run_getter` with any of these:

| Getter                  | Description                                      |
| ----------------------- | ------------------------------------------------ |
| `arp_table`             | ARP table                                        |
| `bgp_config`            | BGP running configuration                        |
| `bgp_neighbors`         | BGP neighbors summary                            |
| `bgp_neighbors_detail`  | BGP neighbors detailed                           |
| `config`                | Running/startup/candidate configuration          |
| `facts`                 | System facts (vendor, model, OS, serial, uptime) |
| `interfaces`            | Interface status and details                     |
| `interfaces_ip`         | IP addresses on interfaces                       |
| `lldp_neighbors`        | LLDP neighbors summary                           |
| `lldp_neighbors_detail` | LLDP neighbors detailed                          |
| `mac_address_table`     | MAC address table                                |
| `ntp_servers`           | NTP server configuration                         |
| `snmp_information`      | SNMP configuration                               |
| `vlans`                 | VLAN information                                 |

---

## Project Structure

```
net-tool/
├── server.py              # FastMCP server, Nornir init, tool definitions
├── pyproject.toml         # Build config, dependencies, tool settings
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
