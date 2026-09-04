# nornir-mcp complete redesign — class-first package architecture

**Supersedes the earlier refactor plan.** The previous version kept the project's
flat single-directory layout and only grouped the 12 tool functions into three
classes. This version is a **complete redesign per best practice**: layered
architecture, where **each tool class is its own self-contained package** (its
own module tree holding the class, its models, and its services), and shared
infrastructure is separated into a `core` kernel that nothing above it may
reach back into. Internal naming and file conventions from the current code are
**not** preserved for their own sake; only externally observable contracts are.

## 0. Mandate

1. Three classes, each a first-class package with its related files:
   `NornirBase` (shared machinery + server/inventory tools), `NapalmTool`
   (NAPALM family), `NetmikoTool` (CLI family).
2. Layered architecture: `core` kernel → `tools/*` class packages → thin
   `server.py` composition root. Dependency arrows point **down only**.
3. Pydantic models live next to their primary consumer (per-class `models.py`),
   not in one global models file.
4. Do not copy the current flat structure "for consistency". Redesign first,
   preserve behavior second.

## 1. Externally observable contracts that stay frozen

These are the only things clients and the safety design depend on — everything
else may move:

1. **Wire tool surface**: the exact 12 tool names (`FROZEN_TOOL_NAMES`) — the
   JSON-RPC schema clients call. Tool **method names** must equal the frozen
   wire names, and each tool's **JSON schema must not change**: FastMCP derives
   it from the method signature, so parameter names, order, defaults, literals
   (`retrieve`, `config_format`), and the `ctx` annotation/position are
   preserved verbatim. Docstrings move verbatim too — FastMCP's tool
   description comes from them.
2. **Behavioral contracts** (all currently test-enforced): §21 envelope
   iff-invariant (`ToolEnvelope.success`), §21.1 byte truncation, §5.3/§13/§14
   policy semantics (canonicalize/classify/abbreviations, default-deny D8,
   DANGEROUS-veto-in-config D2), capability gate, fail-closed pre-change
   backups (D5), execution RLock (D7), audit hash-not-content (§25), immutable
   traversal-safe backups (§19), `_filter_devices` semantics (explicit-empty
   error, no-match ValueError, `filter_func` not `name__in`), and the
   `nr.data.reset_failed_hosts()` reset before every run.
3. **Process entry points**: `nornir_mcp/server.py` must stay at that path
   (CLAUDE.md dev commands `fastmcp dev` / `fastmcp install` reference it), and
   the `nornir-mcp` console script and `python -m nornir_mcp` must keep working.

## 2. Target layout

```
nornir_mcp/
├── __init__.py                  # __version__ only
├── __main__.py                  # python -m nornir_mcp → cli
├── py.typed
├── server.py                    # COMPOSITION ROOT: FastMCP + singletons + tool registration (thin)
├── cli/
│   ├── __init__.py
│   └── main.py                  # argparse CLI  (← nornir_mcp/main.py)
│
├── core/                        # SHARED KERNEL — no tool classes, no FastMCP, no tool-package imports
│   ├── __init__.py
│   ├── envelope.py              # ToolEnvelope/HostOutcome/StructuredError + maybe_truncate  (← responses.py)
│   ├── errors.py                # McpError hierarchy (§22/§23)                                (← errors.py)
│   ├── runner.py                # get_nornir/reset_nornir/EXECUTION_LOCK/NornirLike           (← runner.py)
│   ├── tasks.py                 # _filter_devices/_results_to_outcomes/run_nornir_task        (← tasks.py)
│   ├── policy.py                # canonicalize/classify/rulesets/validate_config_lines         (← policy.py)
│   ├── capability.py            # netmiko_device_type/supports_cli                            (← capability.py)
│   ├── storage.py               # immutable BackupStore/BackupRecord                           (← storage.py)
│   └── audit.py                 # AuditLogger (§25)                                           (← audit.py)
│
└── tools/
    ├── base/                    # ── NornirBase package ──────────────────────────────────
    │   ├── __init__.py          #   re-exports NornirBase
    │   ├── tool.py              #   NornirBase: envelope plumbing + inventory/backup tools
    │   ├── capture.py           #   config-capture service (NAPALM → netmiko fallback)
    │   └── models.py            #   InventoryDevice
    ├── napalm/                  # ── NapalmTool package ──────────────────────────────────
    │   ├── __init__.py          #   re-exports NapalmTool
    │   ├── tool.py              #   NapalmTool(NornirBase): 4 NAPALM-family tools
    │   ├── introspection.py     #   NAPALM driver getter discovery                          (← nornir_mcp/introspection.py)
    │   └── models.py            #   GetterInfo
    └── netmiko/                 # ── NetmikoTool package ──────────────────────────────────
        ├── __init__.py          #   re-exports NetmikoTool
        ├── tool.py              #   NetmikoTool(NornirBase): 4 CLI tools
        ├── gating.py            #   per-host/batch gates + §21.1 output shaping helpers
        ├── tasks.py             #   netmiko_send_commands batch wrapper
        ├── changes.py           #   plan_change/dry_run_outcomes/transcript parsing/pre-change backups
        └── models.py            #   ChangePlan (netmiko change-domain models)
```

Current-file map (nothing is lost; `nornir_mcp/models.py`, `responses.py`,
`changes.py`, `introspection.py`, and `main.py` disappear as such):

| Current file | Becomes |
|---|---|
| `models.py` | split — `InventoryDevice` → `tools/base/models.py`; `GetterInfo` → `tools/napalm/models.py` |
| `responses.py` | `core/envelope.py` (model/class names unchanged) |
| `changes.py` | split — `capture_running_config` (+ napalm/netmiko capture impls) → `tools/base/capture.py`; `ChangePlan` → `tools/netmiko/models.py`; `plan_change`, `dry_run_outcomes`, `capture_pre_change_backups`, `parse_config_transcript`, `CONFIG_ERROR_PATTERNS` → `tools/netmiko/changes.py` |
| `server.py` (994 lines) | shrinks to composition root; tool bodies move to the three `tool.py` files |
| `tasks.py` | `core/tasks.py` minus the `netmiko_send_commands` wrapper (moves to `tools/netmiko/tasks.py`) |
| `main.py` | `cli/main.py`; pyproject `[project.scripts]` → `nornir_mcp.cli.main:main` |

## 3. The three class packages

### 3.1 `tools/base` — `NornirBase`

The shared ancestor and home of the engine-agnostic server tools. Provides the
envelope/selection machinery every tool uses, plus the tools that operate on
inventory, runner cache, and backups.

`tool.py` — `NornirBase`:

- plumbing methods (from current server.py helpers 1–4): `_request_id`,
  `_run_task_envelope`, `_validation_envelope`, `_select_targets`
- tools (4): `nornir_list_inventory`, `nornir_reload_inventory`,
  `nornir_backup_config`, `nornir_list_backups`
- imports only `core.*` (tasks, runner, storage, audit, envelope, errors) and
  its sibling `capture`/`models`

`capture.py` — config-capture service: `capture_running_config()` and the
NAPALM/netmiko capture implementations. Engine-agnostic orchestration (NAPALM
preferred, netmiko fallback) used by **both** `NornirBase.nornir_backup_config`
and `NetmikoTool`'s pre-change backup step, so it must live in the shared base
package, never under `tools/netmiko` (which would invert the dependency).

`models.py` — `InventoryDevice` (moved verbatim).

### 3.2 `tools/napalm` — `NapalmTool(NornirBase)`

`tool.py` — tools (4): `nornir_get_facts`, `nornir_run_getter`,
`nornir_get_config`, `nornir_list_getters`. The first three are thin: build
getter/options payloads and delegate to the inherited `_run_task_envelope`
with `napalm_get`. `nornir_list_getters` delegates to `introspection.py`.

`introspection.py` — getter discovery per platform (`list_getters()`), moved
from the current `nornir_mcp/introspection.py`. Lives here, not core: it is
NAPALM-driver-specific knowledge with a NAPALM-specific model.

`models.py` — `GetterInfo` (moved verbatim).

### 3.3 `tools/netmiko` — `NetmikoTool(NornirBase)`

`tool.py` — tools (4): `nornir_run_command`, `nornir_run_commands`,
`nornir_apply_config`, `nornir_save_config`. Imports netmiko plugin tasks,
`gating`, `changes`, and `core` services.

`gating.py` — CLI-specific helpers moved from server.py (helpers 5–8):
`_gate_host`, `_gate_commands`, `_truncated_command_outputs`,
`_truncated_outputs`.

`tasks.py` — the `netmiko_send_commands` batch wrapper (nornir-netmiko 1.0.x
has no batch plugin). Used only by `nornir_run_commands`.

`changes.py` — write-path orchestration: `plan_change()`,
`dry_run_outcomes()`, `parse_config_transcript()` + `CONFIG_ERROR_PATTERNS`,
`capture_pre_change_backups()` (imports the capture service from
`tools.base.capture`). `_APPLY_OPERATION`/`_CAPTURE_OPERATION` constants move
here / to base capture respectively.

`models.py` — `ChangePlan`. Optional hardening (not required for v1 of the
redesign): typed per-command output and transcript-result models to replace
the ad-hoc dicts — add only if it doesn't slow the migration.

### 3.4 Composition root — `server.py`

The only module that knows FastMCP. Instantiates the three classes (module-level
"shared instances"), registers every tool method on `mcp`, and exports nothing
tool-shaped — the old module-level `server.nornir_get_facts(...)` symbols are
**gone**; tests are rewritten to the class packages (§6).

```python
# server.py (composition root, ~60 lines)
from fastmcp import FastMCP
from nornir_mcp.tools.base.tool import NornirBase
from nornir_mcp.tools.napalm.tool import NapalmTool
from nornir_mcp.tools.netmiko.tool import NetmikoTool

mcp = FastMCP(name="Nornir-NAPALM Server", instructions="...")

# Shared instances (the plan's "shared instance"); classes stay stateless —
# all mutable state remains in core's module singletons (runner cache,
# backup store, audit logger, EXECUTION_LOCK).
base_tools = NornirBase()
napalm_tools = NapalmTool()
netmiko_tools = NetmikoTool()

_TOOLS = (
    base_tools.nornir_list_inventory,
    base_tools.nornir_reload_inventory,
    base_tools.nornir_backup_config,
    base_tools.nornir_list_backups,
    napalm_tools.nornir_get_facts,
    napalm_tools.nornir_run_getter,
    napalm_tools.nornir_get_config,
    napalm_tools.nornir_list_getters,
    netmiko_tools.nornir_run_command,
    netmiko_tools.nornir_run_commands,
    netmiko_tools.nornir_apply_config,
    netmiko_tools.nornir_save_config,
)

for _tool in _TOOLS:                      # bound methods: `self` is bound,
    mcp.tool(name=_tool.__name__)(_tool)  # so the schema has no self param
```

Registration happens on **bound methods after instantiation** — decorating
inside a class body would leak `self` into the tool schema.

## 4. Dependency rules (why there are no cycles)

- `core/*` imports nothing from `tools/*`, `server.py`, or `cli/`. It may import
  third-party libs only (`nornir`, `napalm`, `nornir_netmiko`, pydantic).
- `tools/napalm` → `tools/base` → `core`. `tools/netmiko` → `tools/base` →
  `core`. Sibling tool packages never import each other.
- Shared services used by two tool classes live in `tools/base` (inheritance)
  or `core` — never in one child package. (`capture.py` is the concrete case;
  `changes.py` must not import `napalm_tool` or vice versa.)
- `server.py` imports the three tool packages and is imported by `cli`/`__main__`
  only. Nothing under `core` or `tools` imports `server.py`.

## 5. Tool → class → package map (all 12, wire names frozen)

| Tool | Engine | Package / class | Key imports |
|---|---|---|---|
| `nornir_list_inventory` | — | `tools.base` / `NornirBase` | core runner, `InventoryDevice` |
| `nornir_get_facts` | NAPALM | `tools.napalm` / `NapalmTool` | `napalm_get` |
| `nornir_run_getter` | NAPALM | `tools.napalm` / `NapalmTool` | `napalm_get`, getter normalization |
| `nornir_get_config` | NAPALM | `tools.napalm` / `NapalmTool` | `napalm_get`, sanitized config options |
| `nornir_list_getters` | NAPALM | `tools.napalm` / `NapalmTool` | `introspection.list_getters` |
| `nornir_reload_inventory` | — | `tools.base` / `NornirBase` | `core.runner.reset_nornir` |
| `nornir_run_command` | netmiko | `tools.netmiko` / `NetmikoTool` | `netmiko_send_command`, gating |
| `nornir_run_commands` | netmiko | `tools.netmiko` / `NetmikoTool` | `tasks.netmiko_send_commands`, gating |
| `nornir_backup_config` | NAPALM→netmiko | `tools.base` / `NornirBase` | `capture.capture_running_config`, storage, audit |
| `nornir_list_backups` | — | `tools.base` / `NornirBase` | core storage |
| `nornir_apply_config` | netmiko | `tools.netmiko` / `NetmikoTool` | `changes.*`, `base.capture`, netmiko plugin |
| `nornir_save_config` | netmiko | `tools.netmiko` / `NetmikoTool` | `netmiko_save_config`, capability gate |

## 6. Test restructure (mirror the layout)

The old `tests/test_server.py` monolith (1031 lines) is split by package; the
rest of the suite moves alongside the code it tests. Imports that referenced
top-level modules (`from nornir_mcp import runner, storage, ...`) follow the
files into `core`.

```
tests/
├── conftest.py                      # fakes + fixtures (patch targets updated, see below)
├── test_cli.py                      # entry points (unaffected logic)
├── test_e2e.py                      # wire-level; Client(server.mcp); tool-surface pin stays here
├── core/
│   ├── test_envelope.py             # ← test_responses.py
│   ├── test_errors.py  test_runner.py  test_locking.py  test_tasks.py
│   ├── test_policy.py  test_capability.py  test_storage.py  test_audit.py
├── tools/
│   ├── base/
│   │   ├── test_tool.py             # inventory + reload + backup/list_backups sections of test_server.py
│   │   └── test_capture.py          # capture tests extracted from test_changes.py
│   ├── napalm/
│   │   ├── test_tool.py             # get_facts/run_getter/get_config/list_getters sections
│   │   └── test_introspection.py    # ← test_introspection.py
│   └── netmiko/
│       ├── test_tool.py             # run_command(s)/apply/save sections of test_server.py
│       └── test_changes.py          # ← test_changes.py (minus capture tests)
```

### 6.1 Monkeypatch anchors move with the code

Python resolves a patched name in the module whose global the executing code
looks up — so each anchor moves to the module that now owns the call site:

| Anchor (today) | Test / fixture | New anchor |
|---|---|---|
| `nornir_mcp.server.netmiko_send_command` / `netmiko_send_config` / `netmiko_save_config` | `netmiko_fakes` (conftest) | `nornir_mcp.tools.netmiko.tool` (the module importing netmiko plugin tasks); keep `raising=False` |
| `nornir_mcp.server.run_nornir_task` (spy) | `test_run_getter_normalizes_name_and_option_keys` | `nornir_mcp.tools.base.tool` — `_run_task_envelope` resolves it there |
| `nornir_mcp.server.capture_pre_change_backups` | `test_apply_backup_failure_blocks_apply_devices_untouched` | `nornir_mcp.tools.netmiko.tool` |
| `nornir_mcp.server.netmiko_save_config` | `test_save_config_partial_failure_invariant` | `nornir_mcp.tools.netmiko.tool` |
| `nornir_mcp.changes.napalm.get_network_driver` / `nornir_mcp.changes.netmiko_send_command` | backup-fallback tests (`test_backup_config_napalm_unavailable_*`) | `nornir_mcp.tools.base.capture` (capture now lives there) |
| `nornir_mcp.changes.*` | `tests/test_changes.py` | `nornir_mcp.tools.netmiko.changes` + `nornir_mcp.tools.base.capture` |
| `runner.InitNornir` (`fake_nornir`) | every test via conftest | `nornir_mcp.core.runner` (unchanged semantics) |
| `server.mcp`, `server.<tool>` | test_e2e / test_server | `server.mcp` unchanged; direct tool calls rewritten to class packages, e.g. `NapalmTool().nornir_run_getter(...)` / the `server` singletons |

`FakeNornir`'s `fake_netmiko_*` name dispatch keeps working — the fake task
objects just come from `tools.netmiko.tool` globals now. Direct-call tests
should target the **shared instances in `server.py`** (single source of truth
for test identity) rather than fresh instances, so docstring/signature
assertions match what is registered.

## 7. Migration phases (keep the suite green at every step)

The redesign lands in dependency order: core first, then the class packages,
then the composition root and test rewrite, then deletion of legacy files.
Phases may be committed separately; each must end with the full suite green.

- [ ] **Phase 0 — baseline.** `uv run pytest`, `uv run ruff check .`,
  `uv run mypy .` all green; commit the current state as the reference point.
- [ ] **Phase 1 — core kernel.** Create `core/`; move `runner.py`, `tasks.py`,
  `policy.py`, `capability.py`, `storage.py`, `audit.py`, `errors.py`,
  `responses.py`→`envelope.py` and their tests. Keep `nornir_mcp` root modules
  as thin re-export shims (`from nornir_mcp.core.xxx import *`) so server.py
  and remaining tests import unchanged. Green.
- [ ] **Phase 2 — `tools/base` package.** Split `models.py` (move
  `InventoryDevice`); extract `capture.py` from `changes.py` (leave
  `changes.py` re-exporting `capture_running_config` for now); define
  `NornirBase` in `tool.py` with the 4 server tools, moving bodies verbatim
  (docstrings/signatures intact); root `server.py` re-exports the tool names
  so direct-call tests still pass. Green.
- [ ] **Phase 3 — `tools/napalm` package.** `NapalmTool` + `introspection.py`
  + `GetterInfo`; move `napalm_get` import out of server.py. Update the
  `run_nornir_task` spy anchor (§6.1) and re-export from server.py. Green.
- [ ] **Phase 4 — `tools/netmiko` package.** `NetmikoTool` + `gating.py` +
  `tasks.py` + `changes.py` + `ChangePlan`; retarget the `netmiko_fakes`
  fixture and the apply/save anchors (§6.1); `server.py` imports netmiko
  plugin tasks no longer exist — verify nothing stale is patched. Green (full
  suite — this phase surfaces any missed anchor).
- [ ] **Phase 5 — composition root & test rewrite.** Slim `server.py` to §3.4
  (registration loop, no tool re-exports); split `tests/test_server.py` into
  the §6 tree; rewrite direct calls to the `server` singletons; move the tool-
  surface pin test to `tests/test_e2e.py` (wire-level); move `main.py` →
  `cli/main.py` and update pyproject `[project.scripts]` + `__main__.py`.
  Green.
- [ ] **Phase 6 — delete legacy shims; docs.** Remove root re-export shims
  (`nornir_mcp/responses.py`, `changes.py`, `models.py`, `introspection.py`,
  `main.py` stubs) and `core/tasks.py`'s old netmiko wrapper if duplicated.
  Update `CLAUDE.md` (architecture, testing approach, netmiko_fakes targets,
  tool-definition location, dev commands if they changed) and `README.md`.
- [ ] **Final gates.** `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy .`,
  `uv run vulture nornir_mcp tests --min-confidence 80`,
  `uv run pytest --cov=nornir_mcp --cov-branch`. Confirm the 12-tool surface
  test, signature/docstring tests, and e2e pass unchanged.

**Definition of done:** three class packages, each with its related files;
`core` with no upward imports; `server.py` reduced to composition;
all behavioral tests green with unchanged wire contracts; legacy flat files
gone; docs updated.

## 8. Risks, decisions, and open items

- **Wire schema must not drift.** Moving methods changes nothing FastMCP
  sees *if* docstrings and signatures move verbatim and registration binds
  `self`. Phase 5 includes a schema spot-check (one tool per family compared
  pre/post via `server.mcp.list_tools()`).
- **Anchor churn is the main cost.** Every module move silently breaks
  monkeypatches that targeted the old path. §6.1 is the checklist; Phase 4 is
  where a missed anchor first shows up (expected failure mode: fakes never
  invoked, tests hang or hit real task names).
- **Full test rewrite is intentional.** No module-level `server.<tool>` shims
  survive — the old import surface is part of what the redesign removes.
  `FROZEN_TOOL_NAMES` stays, but moves to the wire-level test file.
- **Keep classes stateless.** Instances in `server.py` are pure grouping
  objects; state stays in `core`'s cached singletons and `EXECUTION_LOCK`.
  Do not reintroduce per-instance Nornir state (D7).
- **`vulture` false positives.** Tool methods registered only via the
  `_TOOLS` tuple and re-exported names in `__init__.py` files may be flagged;
  treat as documented (same category as the conftest side-effect fixtures).
- **Decisions already made** (flag if you disagree): `capture.py` in
  `tools/base` rather than `core` (it couples to NAPALM/netmiko plugins and is
  shared by base + netmiko); `nornir_list_getters` + introspection under
  `tools/napalm`; `ChangePlan` under `tools/netmiko/models.py`; `responses.py`
  renamed `core/envelope.py`; netmiko batch wrapper moved to
  `tools/netmiko/tasks.py`.
- **Open items for the implementer**: whether to add typed output/transcript
  models in `tools/netmiko/models.py` (optional hardening, skip if it slows the
  move); final docstring rewording of moved modules; whether `test_server.py`
  splits should also carve out a shared `tests/tools/base/test_envelope_utils.py`.
