# CLAUDE.md

Agent instruction file for the nornir-mcp codebase.

**`README.md` is the single source of truth** for what this project is, its
features, safety model, setup, usage, and the full development workflow —
read it when a task touches those areas. This file adds only what the README
does not: how the code is organized for editing, the invariants that must
never regress, and the testing anchors that break when code moves. Content is
deliberately **not duplicated** between the two files.

## Verification gates (run before finishing)

- Tests: `uv run pytest`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format --check .`
- Types: `uv run mypy .`
- Dead code: `uv run vulture nornir_mcp tests --min-confidence 80`
  - Side-effect fixtures (e.g. `fake_nornir` requested only to trigger a
    monkeypatch) are flagged as false positives — use
    `request.getfixturevalue(...)` / `@pytest.mark.usefixtures(...)`.

Coverage, dependency management, and dev-server commands live in
`README.md` → Contributing.

## Architecture (how the code is organized for editing)

Class-first layered design — each tool class is its own package; all
dependencies point down: `core` ← `tools/*` ← `server.py`.

`CoreBase (core/base.py)` → `NornirBase (tools/base/tool.py)` →
`NapalmTool (tools/napalm/tool.py)` / `NetmikoTool (tools/netmiko/tool.py)`.

- `server.py` — composition root (~40 lines): instantiates the three classes,
  registers the 12 bound methods on `mcp`, exports nothing tool-shaped. The
  only module that knows FastMCP. Tests target the shared instances
  (`server._nornir_base`, `server._napalm_tools`, `server._netmiko_tools`)
  for direct-call identity (§6.1).
- `cli/main.py` — argparse CLI (`nornir-mcp` script + `python -m nornir_mcp`).
- `core/` — shared kernel, no FastMCP, no registerable tools:
  - `base.py` — `CoreBase` owns the envelope/selection plumbing:
    `_request_id`, `_run_task_envelope`, `_validation_envelope`, `_select_targets`
  - `runner.py` — cached `get_nornir()`/`reset_nornir()`, `EXECUTION_LOCK` (D7),
    `NornirLike` protocol · `tasks.py` — `_filter_devices()`,
    `run_nornir_task()`, `_results_to_outcomes()`
  - rest: `policy`, `capability`, `storage`, `audit`, `errors`, `envelope` —
    stateless services
- `tools/base/` — `NornirBase`: inventory, reload, backups; `capture.py`
  config-capture service shared by base + netmiko; `InventoryDevice` lives in
  `tool.py` beside its only consumer
- `tools/napalm/` — `NapalmTool`: getters/facts/config; `introspection.py`
  getter discovery + `GetterInfo`
- `tools/netmiko/` — `NetmikoTool`: CLI run/apply/save, private gating
  methods (`_gate_host`, `_gate_commands`, `_truncated_*`),
  `netmiko_send_commands` wrapper; `changes.py` — `plan_change`/`ChangePlan`,
  fail-closed pre-change backups, `dry_run_outcomes`, `parse_config_transcript`

**Granularity rules (§4.1):** behavior a class applies to its own calls →
private methods on that class; pure tested services and Nornir task callables
→ module functions; a model lives beside its only consumer — there is
deliberately **no `models.py` anywhere**. Stateless core services are
*imported*, never stored on `self` — a second instance would bypass the global
lock/cache (D7).

## Tool surface (frozen — do not change)

Exactly 12 tools. Wire names are frozen by `FROZEN_TOOL_NAMES` in
`tests/test_e2e.py` (enforced by
`test_e2e_tool_registry_has_exactly_twelve_nornir_tools`). Tool **method
names** equal the wire names, and FastMCP derives the JSON schema from the
signature — preserve parameter names/order/defaults and docstrings verbatim.
`ctx` differs by family: NAPALM tools take required `ctx: Context` first;
netmiko tools take optional `ctx: Context | None = None` last — do not unify.

| Spec tool | Implementation | Spec tool | Implementation |
|---|---|---|---|
| `network_run_command` | `nornir_run_command` | `network_backup_config` | `nornir_backup_config` |
| `network_run_commands` | `nornir_run_commands` | `network_apply_config` | `nornir_apply_config` |
| | | `network_save_config` | `nornir_save_config` |

Deferred spec tools (diff, rollback, approval) follow the same `nornir_` convention.

## Invariants (never regress)

- **Envelope iff-invariant (D4, §21)** — `ToolEnvelope.success` is True iff
  no request-level `error` and every host outcome succeeded. Never optimistic.
- **Truncation (§21.1)** — `maybe_truncate()` is the single choke point for
  output (UTF-8-safe byte truncation, `truncated` flag, budget from
  `NORNIR_MCP_MAX_OUTPUT_BYTES`, default 65536).
- **Policy (D1/D2/D8)** — rulesets exist for `ios`/`eos` only; anything else
  classifies UNKNOWN and fails closed (never fall through to IOS rules). Read
  tools allow READ_ONLY + SAFE_OPERATIONAL only; `nornir_apply_config` vetoes
  DANGEROUS/BLOCKED only (CONFIGURATION/UNKNOWN pass and fail on-device);
  DANGEROUS rejected by every tool; BLOCKED never runs.
- **Fail-closed pre-change backup (D5)** — no `backup` flag; `nornir_apply_config`
  always captures a pre-change backup first; a failed backup means the device
  is never touched and the envelope carries a `backup`-type error. Backups are
  immutable (0600/0700) and always retained.
- **Execution RLock (D7)** — `EXECUTION_LOCK` in `core/runner.py` serializes
  device-touching runs; per-host locking is a deferred extension.
- **Audit (D9, §25)** — hash-not-content; audit `user` is `getpass.getuser()`
  (weak stdio identity, best-effort only).
- **Transcript parsing (D10)** — honest heuristic: no error-pattern match
  means "no error detected", never "verified good"; `device_state` stays
  "unknown" unless a read-back happened (it doesn't in v1).

The user-facing safety model is in `README.md` → Safety model.

## Testing (anchors move with the code)

- Test tree mirrors the package layout: `tests/core/` (kernel),
  `tests/tools/{base,napalm,netmiko}/` (tools), plus `test_cli.py` and
  `test_e2e.py` (wire-level; tool-surface pin).
- `conftest.py` stubs Nornir: tests monkeypatch `InitNornir` and use
  spine-01/leaf-01 fixtures. **`FakeNornir` must model the `GlobalState.data`
  surface** — `run_nornir_task()` calls `nr.data.reset_failed_hosts()`, so
  `FakeNornir` carries a `data` attribute with that method.
- Python resolves a patched name in the module whose global the executing code
  looks up — **monkeypatch anchors move with the code**:
  - `netmiko_fakes` fixture (conftest) → `nornir_mcp.tools.netmiko.tool`
    (module importing `netmiko_send_command`/`netmiko_send_config`/
    `netmiko_save_config`/`netmiko_send_commands`)
  - `run_nornir_task` spies → `nornir_mcp.core.base` (via `_run_task_envelope`)
    and `nornir_mcp.tools.netmiko.tool` (direct calls)
  - `capture_pre_change_backups` / apply / save patches →
    `nornir_mcp.tools.netmiko.tool`
  - capture fallback patches (`napalm.get_network_driver`,
    `netmiko_send_command`) → `nornir_mcp.tools.base.capture`
  - `InitNornir` (`fake_nornir`) → `nornir_mcp.core.runner`
- e2e (`tests/test_e2e.py`): `fastmcp.Client` in-memory;
  `call_tool(...).data` is a `Root` wrapper — convert with
  `dataclasses.asdict()`; the §21 `success` property is not serialized, so
  tests derive it.

## Gotchas

- **Abbreviations are a curated table, not a parser** (`policy._expand`):
  unknown abbreviations classify UNKNOWN and are rejected. Extend
  `PlatformRuleset.abbreviations`, don't add parse logic.
- **`nr.filter(name__in=[...])` silently returns empty** in Nornir 3.5.0 —
  use `nr.filter(filter_func=lambda h: h.name in [...])`. `FakeNornir`
  supports both, but real Nornir only honors `filter_func` for host matching.
- **Netmiko transcripts don't echo per-line results** —
  `netmiko_send_config` returns one transcript, which is exactly why
  `parse_config_transcript` is honest-heuristic (D10).

## Data flow

1. Tool called → `run_nornir_task()` resets `failed_hosts` via
   `nr.data.reset_failed_hosts()` (Nornir quarantines failed hosts in the
   long-lived singleton; the reset restores one-shot behavior)
2. `_filter_devices()` narrows by name/group/platform (explicit-empty →
   validation error; no match → request-level `validation` error)
3. Task runs via `nr.run()` — NAPALM getters (read) or netmiko tasks
   (CLI/write, policy- and capability-gated per host); `nornir_apply_config`
   runs under `execution_lock()` with a mandatory pre-change backup
4. `_results_to_outcomes()` normalizes `AggregatedResult` →
   `dict[str, HostOutcome]`
5. FastMCP serializes to JSON; write-path tools also write immutable backups
   and append audit lines

## Type design

- `NornirLike` protocol (`core/runner.py`) — structural interface
  (`inventory`, `filter()`, `run()`) so helpers accept real or fake Nornir
  without importing the concrete class.
- `ToolEnvelope` wraps every tool response (`operation`, `request_id`,
  `results`, optional request-level `error`); `HostOutcome` makes per-host
  success/failure explicit; `StructuredError` carries the §22 fields.

## Error handling conventions

- `McpError` subclasses carry §22 fields and serialize via `to_dict()`;
  retry policy (§23): connection/timeout retryable; auth, policy rejection,
  and configuration errors never.
- `ValidationError`: multi-line/control-char commands, explicitly empty device
  lists, unsafe host/backup identifiers, empty config batches.
- Request-level failures set the envelope `error` with empty `results`;
  per-host failures become failed `HostOutcome`s — one bad device never hides
  the others (§21).
- All errors include actionable next steps (e.g. call
  `nornir_list_inventory` first).

## Dependencies

- Core: fastmcp, nornir, nornir-napalm, nornir-netmiko, napalm, pydantic,
  paramiko
- Testing: pytest, pytest-cov · Linting: ruff · Typing: mypy ·
  Dead code: vulture

## Deferred roadmap (do NOT implement)

Approval workflow + persistent approval state (§15.2); rollback tool (add
`BackupStore.get(host, backup_id)` — immutable backups are the substrate);
post-change validation (read-back after apply); TextFSM/Genie structured
parsing; YAML externalization of policy rulesets; HTTP transport identity
(stronger than stdio `getpass`); per-host locking (D7 extension); additional
platform rulesets — add a `RULESETS` entry, never assume IOS rules apply
(§13.2).

## Companion projects

- **netlab-demo** (`~/Documents/netlab-demo`): Containerlab test lab with real
  Cisco CSR1000v + Arista cEOS devices for integration testing against live
  devices. Deploy with `containerlab deploy -t lab.clab.yaml`; its `.mcp.json`
  registers this server with `NORNIR_CONFIG` pointing at the lab inventory.
  (User-facing mention: `README.md` → Companion Lab.)
