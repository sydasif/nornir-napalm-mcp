# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Core Workflows

**Test Execution**

- Run all tests: `uv run pytest`
- Run with coverage: `uv run pytest --cov=nornir_mcp --cov-branch`
- Run specific test: `uv run pytest tests/tools/base/test_tool.py::test_reload_inventory`

**Code Quality**

- Lint: `uv run ruff check .`
- Fix lint issues: `uv run ruff check --fix .`
- Type check: `uv run mypy .`
- Format: `uv run ruff format .`
- Dead code scan: `uv run vulture nornir_mcp tests --min-confidence 80`
  - Note: side-effect pytest fixtures (e.g. `fake_nornir` requested only to trigger a monkeypatch) are flagged as false positives; pull them in via `request.getfixturevalue(...)` or class-level `@pytest.mark.usefixtures(...)` instead of unused fixture params

**Dependency Management**

- Sync dependencies: `uv sync`
- Add dependency: `uv add <package>`
- Add dev dependency: `uv add --dev <package>`
- Update lockfile: `uv lock`

**Development Server**

- Local dev (MCP Inspector): `fastmcp dev nornir_mcp/server.py`
- Claude Desktop install: `fastmcp install nornir_mcp/server.py`
- Run STDIO transport: `nornir-mcp --transport stdio`
- Run HTTP transport: `nornir-mcp --transport http --host 0.0.0.0 --port 8000`

## Code Architecture

### Core Components

**nornir_mcp/** - Installable package:

- `__init__.py` - Package version (`__version__`)
- `__main__.py` - Supports `python -m nornir_mcp`
  **nornir_mcp/server.py** — Composition root (~60 lines). Imports three tool classes
  (`NornirBase`, `NapalmTool`, `NetmikoTool`), instantiates them, builds `_TOOLS` tuple,
  registers 12 bound `@mcp.tool()` methods. Module-level aliases point at the same bound
  methods for backward-compatible `server.nornir_*` access. The CLI entry point is
  `nornir_mcp.cli.main:main`.

**nornir_mcp/core/** — Kernel package (no imports from tools/server):

- `__init__.py` — re-exports the common subset for `from nornir_mcp.core import ...`
- `errors.py` — Categorized exceptions (`McpError`, `ErrorType`) with per-class retryable policy (spec §22/§23)
- `envelope.py` — Response envelope models (`ToolEnvelope`, `HostOutcome`, `StructuredError`) and byte-based truncation (spec §21/§21.1)
- `policy.py` — Command input normalization (`canonicalize()`, `MAX_COMMAND_LENGTH`) — newline/control rejection and matching form (spec §5.3/§13/§14)
- `capability.py` — Platform capability gate (`NETMIKO_DEVICE_TYPES`, `netmiko_device_type()`, `supports_cli()`)
- `storage.py` — Immutable backup storage (`BackupStore`, `FilesystemBackupStore`, `get_backup_store()`) — traversal-safe hosts, 0600/0700 perms, metadata sidecars (spec §8.2/§19)
- `audit.py` — Append-only JSONL audit logger (`AuditLogger`, `get_audit_logger()`) — never logs config content (spec §25, D9)
- `runner.py` — Nornir initialization, config loading, caching (`get_nornir()`, `reset_nornir()`); exports `NornirLike` protocol
- `tasks.py` — Task helpers: `_filter_devices()`, `run_nornir_task()`, `_results_to_outcomes()`, `netmiko_send_commands` (local batch shim — nornir-netmiko 1.0.x no longer ships a batch plugin)
- `base.py` — `CoreBase` with `_request_id`, `_run_task_envelope`, `_validation_envelope`, `_select_targets`

**nornir_mcp/tools/base/**:

- `__init__.py`
- `tool.py` — `NornirBase(CoreBase)` with 4 server tools + `InventoryDevice` model
- `capture.py` — `capture_running_config()` — shared NAPALM/CLI capture (spec §8.2/§9)

**nornir_mcp/tools/napalm/**:

- `__init__.py`
- `tool.py` — `NapalmTool(NornirBase)` with 4 NAPALM tools
- `introspection.py` — NAPALM getter discovery per platform (`list_getters()`, `GetterInfo`)

**nornir_mcp/tools/netmiko/**:

- `__init__.py`
- `tool.py` — `NetmikoTool(NornirBase)` with 4 CLI/write tools
- `changes.py` — Write-path orchestration: `ChangePlan`, `plan_change()`, `capture_pre_change_backups()` (fail-closed §8.2), `dry_run_outcomes()` (§16.1), `parse_config_transcript()` (D10 honest transcript heuristic — never reports a detected error as success, §8.3)

## Tool Naming (D3)

All tools use the `nornir_` prefix. The six pre-existing names
(`nornir_list_inventory`, `nornir_get_facts`, `nornir_run_getter`,
`nornir_get_config`, `nornir_list_getters`, `nornir_reload_inventory`)
were **never changed** — existing clients (netlab-demo `.mcp.json`, saved
workflows) reference them. Spec tool names map to the implementation:

| Spec tool               | Implementation         |
| ----------------------- | ---------------------- |
| `network_run_command`   | `nornir_run_command`   |
| `network_run_commands`  | `nornir_run_commands`  |
| `network_backup_config` | `nornir_backup_config` |
| `network_apply_config`  | `nornir_apply_config`  |
| `network_save_config`   | `nornir_save_config`   |

Deferred spec tools (diff, rollback, approval) will follow the same
`nornir_` convention. The exact 12-tool surface is enforced by
`test_e2e_tool_registry_has_exactly_twelve_nornir_tools` (`FROZEN_TOOL_NAMES`
in `tests/test_e2e.py`) — any rename or addition fails CI.

**Testing Approach** (`tests/` directory):

- `conftest.py` - Pytest fixtures that stub Nornir for isolated testing
- `tests/tools/base/test_tool.py` - Unit tests for NornirBase tools (inventory, reload, backup, list backups, run_commands)
- `tests/tools/napalm/test_napalm.py` - Unit tests for NAPALM tools (facts, getters, config, list_getters)
- `tests/tools/netmiko/test_netmiko.py` - Unit tests for Netmiko tools (run_command, apply_config, save_config)
- `test_policy.py` - Unit tests for command canonicalization/classification
- `test_capability.py` - Unit tests for the capability gate and conftest netmiko fakes
- `test_storage.py` - Unit tests for immutable backup storage
- `test_audit.py` - Unit tests for the audit logger
- `test_tasks.py` - Unit tests for device filtering and task execution
- `test_cli.py` - Unit tests for the CLI entry points
- `test_runner.py` - Unit tests for config loading and path expansion
- `test_locking.py` - Unit tests for the global `EXECUTION_LOCK` (held during runs, reentrant, always released)
- `test_changes.py` - Unit tests for change planning, fail-closed pre-change backups, dry-run labeling, transcript parsing
- `test_e2e.py` - End-to-end tests through the real MCP protocol layer (`fastmcp.Client` in-memory; `@pytest.mark.anyio`, `anyio_backend` fixture in conftest). `call_tool(...).data` is a `Root` wrapper — convert with `dataclasses.asdict()`; the §21 `success` property is not serialized, so tests derive it
- Tests use monkeypatching to replace `InitNornir` with fake inventory
- Test data includes spine-01 and leaf-01 devices for consistent assertions
- **`FakeNornir` must model the `GlobalState.data` surface** — since `tasks.run_nornir_task()` calls `nr.data.reset_failed_hosts()`, `FakeNornir` carries a `data: FakeGlobalState` attribute (with a `reset_failed_hosts()` method). When touching `run_nornir_task`, keep `FakeGlobalState` in sync with what production code invokes on `nr.data`.
- **Gotcha**: `nr.filter(name__in=[...])` silently returns empty in Nornir 3.5.0. Use `nr.filter(filter_func=lambda h: h.name in [...])` instead. The `FakeNornir` in `conftest.py` supports both, but real Nornir only handles `filter_func` correctly for hostname matching. Always verify filter changes against a real Nornir instance.

### Key Design Patterns

1. **Lazy Initialization**: Nornir instance is created only when first needed (`get_nornir()`), allowing server to start even with broken inventory
2. **Singleton Caching**: Module-level `@cache` on `get_nornir()` ensures a single Nornir instance reused across requests, avoiding repeated YAML parsing and connection setup
3. **Failed-device Reset**: `nr.data.reset_failed_hosts()` is called before every task. Nornir quarantines hosts that fail in `GlobalState.failed_hosts` (in `nornir/core/__init__.py:151`). In a long-lived singleton server this persists across calls; the reset makes every request start with all devices available, matching the behavior of a one-shot script (`InitNornir()` fresh per invocation)
4. **Device Filtering**: `_filter_devices()` provides consistent name/group/platform filtering across all tools
5. **Configuration Override**: `NORNIR_CONFIG` environment variable allows custom config paths
6. **Transport Flexibility**: Supports both STDIO (Claude Desktop) and HTTP (network) transports
7. **Installable Package**: Proper Python package structure for `uvx` execution from GitHub
8. **Default-Deny Per-Platform Policy (D8)**: rulesets exist for `ios` and `eos` **only**; any other platform classifies UNKNOWN — never fall through to IOS rules (spec §13.2). Unknown platforms and unknown abbreviations fail closed.
9. **Category Decision Table (D1/D2)**: read tools (`nornir_run_command` / `nornir_run_commands`) allow READ_ONLY + SAFE_OPERATIONAL only. `nornir_apply_config` vetoes DANGEROUS and BLOCKED lines only — CONFIGURATION/UNKNOWN lines pass (bad sub-commands fail on-device and surface in transcript parsing; vetoing UNKNOWN would make the tool unusable). DANGEROUS is rejected by every tool pending spec M4 approval; BLOCKED is never runnable.
10. **Fail-Closed Pre-Change Backup (D5)**: there is **no** `backup` flag — `nornir_apply_config` always captures a pre-change backup before any device write; if any backup fails, the device is NOT touched and the request returns a `backup`-type envelope error. Backups are immutable (0600 files / 0700 dirs) and always retained.
11. **Execution RLock (D7)**: a global reentrant `EXECUTION_LOCK` in `runner.py` serializes all device-touching runs (FastMCP threadpool vs process-wide Nornir singleton racing on GlobalState). Correctness-first tradeoff: concurrency _within_ a run is preserved via Nornir workers; per-host locking is a documented future extension.
12. **Envelope iff-Invariant (D4)**: `ToolEnvelope.success` is True iff there is no request-level `error` **and** every host outcome succeeded. Tools set `success=False` on any per-host failure — never optimistic.
13. **Truncation Contract (§21.1)**: `maybe_truncate()` is the single choke point for output — UTF-8-safe byte truncation with an explicit `truncated` flag and `original_size`; budget from `NORNIR_MCP_MAX_OUTPUT_BYTES` (default 65536).
14. **Audit Hash-Not-Content**: the audit log carries operation, request id, change id, hosts, result, and sha256s — **never config text** (spec §25). Raw config lives only in the immutable backups.
15. **Weak Stdio Identity (D9, §15.1)**: audit `user` is `getpass.getuser()` — documented as a weak best-effort marker because stdio MCP has no strong authenticated identity.

### Gotchas

- **Abbreviation expansion is a curated table, not a parser**: `policy._expand` knows specific forms (`wr e`, `conf t`, `rel`); an abbreviation missing from the table classifies by its literal tokens — almost always UNKNOWN, i.e. rejected. Fail-closed by design; extend `PlatformRuleset.abbreviations` rather than adding parse logic.
- **Transcript error detection is heuristic (D10)**: `parse_config_transcript` searches known patterns (`CONFIG_ERROR_PATTERNS`); absence of a match means "no error detected", never "verified good" — `device_state` is "unknown" unless a read-back happened (it doesn't, in v1). A host with a detected error is never reported as success (spec §8.3).
- **Netmiko transcripts do not echo per-line results**: `netmiko_send_config` returns one transcript string, not per-line pass/fail — which is exactly why transcript parsing is honest-heuristic rather than authoritative.

### Deferred Roadmap (do NOT implement)

- Approval workflow + persistent approval state (spec §15.2)
- Rollback tool — add a `BackupStore.get(host, backup_id)` read method (the natural extension point on the `BackupStore` protocol); the immutable backups are already the rollback substrate
- Post-change validation (read-back after apply)
- TextFSM/Genie structured parsing of CLI output
- YAML externalization of policy rulesets
- HTTP transport identity (stronger than stdio's `getpass`)
- Per-host locking (D7 future extension)
- Additional platform rulesets — add a new entry to `RULESETS`; never assume IOS rules apply to a new platform (spec §13.2)

### Data Flow

Read path:

1. MCP tool called with name/group/platform filters
2. `core.tasks.run_nornir_task()` resets `failed_hosts` via `nr.data.reset_failed_hosts()`
3. `core.tasks._filter_devices()` narrows inventory to matching devices
4. The requested task executes via Nornir's `nr.run()` — NAPALM getters for read tools, netmiko tasks for CLI/write tools. CLI/write paths are policy- and capability-gated first (per host); `nornir_apply_config` additionally runs under `execution_lock()` with a mandatory pre-change backup
5. `core.tasks._results_to_outcomes()` normalizes `AggregatedResult` → `dict[str, HostOutcome]`
6. FastMCP automatically serializes to JSON; write-path tools also write immutable backups and append audit JSONL lines

### Type Design

- `NornirLike` protocol in `core/runner.py` defines the structural interface (`inventory`, `filter()`, `run()`) for task helpers, enabling injection of real or fake Nornir without importing the concrete class.
- `ToolEnvelope` wraps every tool response: `operation`, `request_id`, `results: dict[str, HostOutcome]`, optional request-level `error`; its `success` property implements the §21 invariant (True iff no request-level error and every host outcome succeeded). `HostOutcome` makes per-host success/failure explicit. `StructuredError` carries the §22 fields (`type`, `message`, `host`, `operation`, `retryable`).

### Dependencies

- Core: fastmcp, nornir, nornir-napalm, nornir-netmiko, napalm, pydantic, paramiko
- Testing: pytest, pytest-cov
- Linting: ruff
- Typing: mypy
- Dead code: vulture

## Companion Projects

- **netlab-demo** (`~/Documents/netlab-demo`): Containerlab test lab with real Cisco CSR1000v + Arista cEOS devices. Deploy with `containerlab deploy -t lab.clab.yaml`. Its `.mcp.json` registers this server with `NORNIR_CONFIG` pointing to the lab's inventory. Use it for integration testing against live devices.

## Error Handling Conventions

- **Categorized `McpError` subclasses** (`errors.py`) carry the §22 fields — `type`, `message`, `host`, `operation`, `retryable` — and serialize via `to_dict()`. Retry policy (§23): connection/timeout are retryable; auth, policy rejection, and configuration errors never are.
- **`ValidationError`**: invalid input — multi-line/control-character commands, explicitly empty device lists, unsafe host/backup identifiers, empty config batches.
- **`ValueError` from `_filter_devices`** (no devices match the filters): every tool wraps it into a request-level `validation` StructuredError on the envelope instead of raising.
- **Request-level vs per-host**: request-level failures set the envelope's `error` with empty `results`; per-host failures become failed `HostOutcome`s so one bad device never hides the others (§21).
- All errors include actionable messages suggesting next steps (e.g., call nornir_list_inventory first).
