# Nornir-NAPALM FastMCP Server

A [FastMCP](https://github.com/jlowin/fastmcp) server that exposes live network device state to AI assistants (Claude, etc.) via NAPALM getters. Nornir handles inventory loading and concurrent device connections.

All operations are **read-only**. No configuration push is exposed.

---

## Architecture

```
MCP Client (Claude Desktop / Claude.ai)
        │  STDIO or SSE
        ▼
  FastMCP Server (server.py)
        │  InitNornir
        ▼
  Nornir (threaded runner, SimpleInventory)
        │  YAML files
        ▼
  inventory/{hosts,groups,defaults}.yaml
        │  napalm_get task
        ▼
  NAPALM Drivers (EOS, JunOS, IOS, …)
        │  SSH / eAPI / NETCONF
        ▼
  Network Devices
```

---

## Quickstart

### 1. Install

```bash
# Clone / copy this project
cd nornir-napalm-mcp

# Create a virtual environment
python -m venv .venv && source .venv/bin/activate

# Install with the drivers you need
pip install ".[eos]"         # Arista EOS
pip install ".[junos]"       # Juniper JunOS
pip install ".[ios]"         # Cisco IOS/IOS-XE
pip install ".[all-drivers]" # All of the above
```

### 2. Configure your inventory

Edit `inventory/hosts.yaml`, `groups.yaml`, and `defaults.yaml` to match your environment. See the sample files for the expected format.

> **Security note:** Do not commit credentials to version control. Use environment variables or a secrets manager (see [Credential Management](#credential-management)).

### 3. Run locally (MCP Inspector)

```bash
fastmcp dev server.py
```

### 4. Install into Claude Desktop

```bash
fastmcp install server.py
```

This writes the STDIO entry into `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent path on your OS.

---

## MCP Tools

| Tool | Description |
|---|---|
| `list_inventory` | List all devices with hostname, platform, and groups |
| `get_network_facts` | System facts: vendor, model, OS version, serial, uptime |
| `get_network_interfaces` | Interface state + IP assignments |
| `run_napalm_getter` | Generic: run any NAPALM getter by name |

### Common getters for `run_napalm_getter`

`arp_table`, `bgp_neighbors`, `bgp_neighbors_detail`, `bgp_config`,
`environment`, `lldp_neighbors`, `lldp_neighbors_detail`,
`mac_address_table`, `ntp_servers`, `ntp_stats`,
`optics`, `route_to`, `snmp_information`, `users`, `vlans`

---

## Docker Deployment

```bash
# Build
docker build -t nornir-napalm-mcp .

# Run — mount your inventory directory
docker run -p 8000:8000 \
  -v $(pwd)/inventory:/app/inventory \
  nornir-napalm-mcp
```

The container starts an SSE server on port 8000 by default.

### docker-compose example

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

---

## Configuration

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `NORNIR_CONFIG` | `config.yaml` | Path to the Nornir config file |

### Changing transport

```bash
# STDIO (default, for Claude Desktop)
python server.py --transport stdio

# SSE (for network-accessible deployments)
python server.py --transport sse --host 0.0.0.0 --port 8000
```

---

## Credential Management

Storing plaintext credentials in YAML is fine for labs. For production:

**Option A — Environment variables via nornir-env-transform**

```yaml
# defaults.yaml
username: "{{ env['NET_USERNAME'] }}"
password: "{{ env['NET_PASSWORD'] }}"
```

**Option B — HashiCorp Vault** via `nornir-vault` plugin.

**Option C — Per-host secrets** using `nornir-secrets` or a custom transform.

---

## Post-MVP Roadmap

- [ ] Safe config push (`napalm_configure` with mandatory dry-run)
- [ ] Group-based bulk queries ("get ARP for all spines")
- [ ] NetBox inventory backend (`nornir-netbox`)
- [ ] Connection pooling / persistent NAPALM connections
- [ ] Structured error codes surfaced back to the LLM
