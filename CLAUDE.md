# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Core Workflows

**Test Execution**

- Run all tests: `uv run pytest`
- Run with coverage: `uv run pytest --cov=src --cov-branch`
- Run specific test: `uv run pytest tests/test_helpers.py::test_function_name`

**Code Quality**

- Lint: `uv run ruff check .`
- Fix lint issues: `uv run ruff check --fix .`
- Type check: `uv run mypy .`
- Format: `uv run ruff format .`

**Dependency Management**

- Sync dependencies: `uv sync`
- Add dependency: `uv add <package>`
- Add dev dependency: `uv add --dev <package>`
- Update lockfile: `uv lock`

**Development Server**

- Local dev (MCP Inspector): `fastmcp dev server.py`
- Claude Desktop install: `fastmcp install server.py`
- Run STDIO transport: `python server.py --transport stdio`
- Run SSE transport: `python server.py --transport sse --host 0.0.0.0 --port 8000`

## Code Architecture

### Core Components

**models.py** - Pydantic data models:

- `InventoryDevice` - Device name, hostname, platform, and groups
- `GetterInfo` - Available NAPALM getters per platform

**runner.py** - Nornir initialization:

- `_get_nornir()` - `lru_cache` singleton with lazy initialization
- `reset_nornir()` - Clears the cached instance

**server.py** - FastMCP server and tool definitions:

- `_filter_devices()` - Filters Nornir by name, group, or platform
- Eight MCP tools:
  1. `nornir_list_inventory` - Lists all devices from inventory
  2. `nornir_get_facts` - Retrieves device system facts
  3. `nornir_run_getter` - Generic NAPALM getter runner
  4. `nornir_get_config` - Retrieves running/startup configuration
  5. `nornir_run_cli` - Executes CLI commands
  6. `nornir_ping` - Sends ICMP ping from device(s)
  7. `nornir_list_getters` - Lists available getters per platform
  8. `nornir_reload_inventory` - Reloads YAML inventory files
- Main entry point with transport selection

**Testing Approach** (`tests/` directory):

- `conftest.py` - Pytest fixtures that stub Nornir for isolated testing
- `test_helpers.py` - Unit tests covering all MCP tools with mocked Nornir responses
- Tests use monkeypatching to replace `InitNornir` with fake inventory
- Test data includes spine-01 and leaf-01 devices for consistent assertions
- **Gotcha**: `nr.filter(name__in=[...])` silently returns empty in Nornir 3.5.0. Use `nr.filter(filter_func=lambda h: h.name in [...])` instead. The `FakeNornir` in `conftest.py` supports both, but real Nornir only handles `filter_func` correctly for hostname matching. Always verify filter changes against a real Nornir instance.

### Key Design Patterns

1. **Lazy Initialization**: Nornir instance is created only when first needed (`_get_nornir()`), allowing server to start even with broken inventory
2. **Singleton Caching**: `lru_cache(maxsize=1)` ensures single Nornir instance reused across requests
3. **Device Filtering**: `_filter_devices()` provides consistent name/group/platform filtering across all tools
4. **Configuration Override**: `NORNIR_CONFIG` environment variable allows custom config paths
5. **Transport Flexibility**: Supports both STDIO (Claude Desktop) and SSE (network) transports

### Data Flow

1. MCP tool called with name/group/platform filters
2. `_filter_devices()` narrows inventory to matching devices
3. NAPALM task executed via Nornir's `nr.run()`
4. Raw NAPALM response returned as dict
5. FastMCP automatically serializes to JSON

### Dependencies

- Core: fastmcp, nornir, nornir-napalm, napalm
- Testing: pytest, pytest-cov
- Linting: ruff
- Typing: mypy

## Companion Projects

- **nornir-mcp-lab** (`~/Documents/nornir-mcp-lab`): Containerlab test lab with real Cisco CSR1000v + Arista cEOS devices. Deploy with `containerlab deploy -t lab.clab.yaml`. Its `.mcp.json` registers this server with `NORNIR_CONFIG` pointing to the lab's inventory. Use it for integration testing against live devices.

## Error Handling Conventions

- ValueError: Invalid input (unknown device, no matching filters)
- All errors include actionable messages suggesting next steps (e.g., call list_inventory first)

## Known Limitations

- `nornir_ping` on Cisco IOS XE may fail with Netmiko ReadTimeout on slow virtual platforms (CSR1000v). Use `nornir_run_cli(commands=["ping ..."])` as a workaround.
