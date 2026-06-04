# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Core Workflows

**Test Execution**

- Run all tests: `uv run pytest`
- Run with coverage: `uv run pytest --cov=src --cov-branch`
- Run specific test: `uv run pytest tests/test_helpers.py::test_function_name`
- Run single test function: `uv run pytest tests/test_helpers.py::test_nornir_list_inventory`

**Code Quality**

- Lint: `uv run ruff check .`
- Fix lint issues: `uv run ruff check --fix .`
- Type check: `uv run mypy server.py`
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

**server.py** - Main application file containing:

- FastMCP server initialization with lazy-loaded Nornir instance
- Four primary MCP tools:
  1. `nornir_list_inventory` - Lists all devices from inventory
  2. `nornir_get_facts` - Retrieves device system facts (vendor, model, OS, etc.)
  3. `nornir_get_interfaces` - Gets interface and IP address data
  4. `nornir_run_getter` - Generic runner for any NAPALM getter (arp_table, vlans, etc.)
  5. `nornir_reload_inventory` - Reloads YAML inventory files
- Singleton Nornir pattern: `_get_nornir()` caches instance after first initialization
- Error handling with meaningful error messages for missing devices or failed getters

**Testing Approach** (`tests/` directory):

- `conftest.py` - Pytest fixtures that stub Nornir for isolated testing
- `test_helpers.py` - Unit tests covering all MCP tools with mocked Nornir responses
- Tests use monkeypatching to replace `InitNornir` with fake inventory
- Test data includes spine-01 and leaf-01 devices for consistent assertions

### Key Design Patterns

1. **Lazy Initialization**: Nornir instance is created only when first needed (`_get_nornir()`), allowing server to start even with broken inventory
2. **Singleton Pattern**: Global `_nornir` variable ensures single Nornir instance reused across requests
3. **Separation of Concerns**:
   - MCP tool decorators handle API exposure
   - Helper functions (`_run_getter`, `_resolve_config`) encapsulate logic
   - Pydantic models define structured return types
4. **Configuration Override**: `NORNIR_CONFIG` environment variable allows custom config paths
5. **Transport Flexibility**: Supports both STDIO (Claude Desktop) and SSE (network) transports

### Data Flow

1. MCP tool called with device name and/or getter
2. `_get_nornir()` initializes/caches Nornir instance from inventory
3. `_run_getter()` filters to specific device and executes NAPALM getter task
4. Raw NAPALM response processed and returned as Pydantic model
5. FastMCP automatically serializes Pydantic models to JSON

### Dependencies

- Core: fastmcp, nornir, nornir-napalm, napalm
- Testing: pytest, pytest-asyncio, pytest-cov
- Linting: ruff
- Typing: mypy

## Companion Projects

- **nornir-mcp-lab** (`/home/zulu/Documents/nornir-mcp-lab`): Containerlab test lab with real Cisco CSR1000v + Arista cEOS devices. Its `.mcp.json` registers this server with `NORNIR_CONFIG` pointing to the lab's inventory. Use it for integration testing against live devices.

## Error Handling Conventions

- ValueError: Invalid input (unknown device, invalid getter name)
- RuntimeError: NAPALM task failures or unexpected data types
- All errors include actionable messages suggesting next steps (e.g., call list_inventory first)
