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

1. Four classes in one inheritance chain, each a first-class package with
   its related files: `CoreBase` (shared envelope/selection plumbing, in
   `core`), `NornirBase` (server/inventory tools), `NapalmTool` (NAPALM
   family), `NetmikoTool` (CLI family).
2. Layered architecture: `core` kernel → `tools/*` class packages → thin
   `server.py` composition root. Dependency arrows point **down only**.
3. Pydantic models live beside their only consumer — **not** in per-package
   `models.py` files. Create a `models.py` only when a package accumulates
   ≥2 models or one is shared by 2+ modules (§4.1).
4. Do not copy the current flat structure "for consistency". Redesign first,
   preserve behavior second.
5. KISS/DRY granularity: no class holds behavior that belongs to another
   layer, no file exists to hold a single tiny helper or model, and each
   shared behavior is defined exactly once (§4.1).

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
├── core/                        # SHARED KERNEL — stateless services + CoreBase; no FastMCP, no registerable tools
│   ├── __init__.py
│   ├── base.py                  # CoreBase — shared envelope/selection plumbing (private methods, no tools)
│   ├── envelope.py              # ToolEnvelope/HostOutcome/StructuredError + maybe_truncate/outcome_from_mcp_error  (← responses.py)
│   ├── errors.py                # McpError hierarchy (§22/§23)                                (← errors.py)
│   ├── runner.py                # get_nornir/reset_nornir/EXECUTION_LOCK/NornirLike           (← runner.py)
│   ├── tasks.py                 # _filter_devices/_results_to_outcomes/run_nornir_task        (← tasks.py, minus netmiko wrapper)
│   ├── policy.py                # canonicalize/classify/rulesets/validate_config_lines         (← policy.py)
│   ├── capability.py            # netmiko_device_type/supports_cli                            (← capability.py)
│   ├── storage.py               # immutable BackupStore/BackupRecord                           (← storage.py)
│   └── audit.py                 # AuditLogger (§25)                                           (← audit.py)
│
└── tools/
    ├── base/                    # ── NornirBase package ──────────────────────────────────
    │   ├── __init__.py          #   re-exports NornirBase
    │   ├── tool.py              #   NornirBase(CoreBase): 4 server tools + InventoryDevice
    │   └── capture.py           #   config-capture service (NAPALM → netmiko fallback)
    ├── napalm/                  # ── NapalmTool package ──────────────────────────────────
    │   ├── __init__.py          #   re-exports NapalmTool
    │   ├── tool.py              #   NapalmTool(NornirBase): 4 NAPALM-family tools
    │   └── introspection.py     #   getter discovery + GetterInfo model                     (← nornir_mcp/introspection.py)
    └── netmiko/                 # ── NetmikoTool package ──────────────────────────────────
        ├── __init__.py          #   re-exports NetmikoTool
        ├── tool.py              #   NetmikoTool(NornirBase): 4 CLI tools + CLI-gating methods + netmiko_send_commands
        └── changes.py           #   plan_change/dry_run_outcomes/transcript parsing/pre-change backups + ChangePlan
```

Current-file map (nothing is lost; `nornir_mcp/models.py`, `responses.py`,
`changes.py`, `introspection.py`, and `main.py` disappear as such):

| Current file | Becomes |
|---|---|
| `models.py` | dissolved — each model moves beside its only consumer: `InventoryDevice` → `tools/base/tool.py`; `GetterInfo` → `tools/napalm/introspection.py` |
| `responses.py` | `core/envelope.py` (model/class names unchanged) |
| `changes.py` | split — `capture_running_config` (+ napalm/netmiko capture impls) → `tools/base/capture.py`; `ChangePlan`, `plan_change`, `dry_run_outcomes`, `capture_pre_change_backups`, `parse_config_transcript`, `CONFIG_ERROR_PATTERNS` → `tools/netmiko/changes.py` |
| `server.py` (994 lines) | shrinks to composition root; tool bodies move to the class files (`core/base.py` + three `tool.py` files) |
| `tasks.py` | `core/tasks.py` minus the `netmiko_send_commands` wrapper (folds into `tools/netmiko/tool.py`, dropping `core`'s only `nornir_netmiko` import) |
| `main.py` | `cli/main.py`; pyproject `[project.scripts]` → `nornir_mcp.cli.main:main` |

## 3. The class chain: CoreBase → NornirBase → (NapalmTool | NetmikoTool)

### 3.1 `core` — `CoreBase` (the shared base class)

`CoreBase` owns the envelope/selection plumbing every tool uses. The `core`
package's module files remain stateless services; only the plumbing becomes a
class.

`base.py` — `CoreBase` (imports only sibling core modules):

- private plumbing methods (from current server.py helpers 1–4):
  `_request_id`, `_run_task_envelope`, `_validation_envelope`,
  `_select_targets`
- deliberately **no tools** — nothing registerable with FastMCP

Why here, as its own class? Three reasons:

1. **Class→package mandate (§0.1).** `core/` *is* `CoreBase`'s package: its
   related files are the kernel modules the plumbing is built on (`tasks`,
   `runner`, `envelope`, `errors`). The only new file is `base.py`.
2. **Reachability is *not* the reason** (an earlier draft claimed it was —
   `tools/napalm`/`tools/netmiko` already import `tools/base` to inherit
   `NornirBase`, so a sibling import would add nothing new).
3. **The chain is the DRY mechanism of the class design.** Every current and
   *future* tool class gets envelope/selection machinery by inheritance,
   defined once. The deferred roadmap tools (diff, rollback, approval) extend
   `NornirBase` or `NetmikoTool` and inherit the whole chain — no
   re-importing, no copy-paste.

**What stays module-level (and why it must not become instance state):**
`runner` (cached `get_nornir`, `EXECUTION_LOCK`), `storage`, `audit`,
`policy`, `capability`, `tasks`. These are process-wide singletons and pure
functions — D7's lock exists to serialize *across* the FastMCP threadpool, and
the runner cache is deliberately process-global. Owned by an instance, a
second instance would silently bypass the global lock/cache. So `CoreBase`
imports them like any core consumer (`from nornir_mcp.core import runner`),
never stores them on `self`.

### 3.2 `tools/base` — `NornirBase(CoreBase)`

The shared ancestor of the two engine families and home of the engine-agnostic
server tools: inventory listing, runner-cache reset, and backup capture/list.

`tool.py` — `NornirBase(CoreBase)`:

- no plumbing of its own — inherits `_request_id`, `_run_task_envelope`,
  `_validation_envelope`, `_select_targets` from `CoreBase`
- tools (4): `nornir_list_inventory`, `nornir_reload_inventory`,
  `nornir_backup_config`, `nornir_list_backups`
- also defines `InventoryDevice` — `nornir_list_inventory` is its only
  consumer, so it lives here rather than in a one-class `models.py` (§4.1)
- imports only `core.*` and its sibling `capture`

`capture.py` — config-capture service: `capture_running_config()` and the
NAPALM/netmiko capture implementations. Engine-agnostic orchestration (NAPALM
preferred, netmiko fallback) used by **both** `NornirBase.nornir_backup_config`
and `NetmikoTool`'s pre-change backup step, so it must live in the shared base
package, never under `tools/netmiko` (which would invert the dependency).

### 3.3 `tools/napalm` — `NapalmTool(NornirBase)`

`tool.py` — tools (4): `nornir_get_facts`, `nornir_run_getter`,
`nornir_get_config`, `nornir_list_getters`. The first three are thin: build
getter/options payloads and delegate to the inherited `_run_task_envelope`
with `napalm_get`. `nornir_list_getters` delegates to `introspection.py`.

`introspection.py` — getter discovery per platform (`list_getters()`), moved
from the current `nornir_mcp/introspection.py`, plus the `GetterInfo` model it
returns — `introspection.py` is `GetterInfo`'s only consumer, so the model
lives here, not in a one-class `models.py` (§4.1). Lives here, not core: it is
NAPALM-driver-specific knowledge.

### 3.4 `tools/netmiko` — `NetmikoTool(NornirBase)`

`tool.py` — `NetmikoTool(NornirBase)`:

- tools (4): `nornir_run_command`, `nornir_run_commands`,
  `nornir_apply_config`, `nornir_save_config`
- private CLI-gating methods (current server.py helpers 5–8): `_gate_host`,
  `_gate_commands`, `_truncated_command_outputs`, `_truncated_outputs` —
  these shape NetmikoTool's own calls, so they are methods on the class that
  owns them, mirroring CoreBase owning helpers 1–4 (§4.1). No `gating.py`
- module function `netmiko_send_commands` — the batch task wrapper
  (nornir-netmiko 1.0.x has no batch plugin), used only by
  `nornir_run_commands`. It is a Nornir task callable, not class behavior, so
  it stays a function in this module; folding it here drops `core/tasks.py`'s
  only `nornir_netmiko` import. No `tasks.py`
- imports netmiko plugin tasks, sibling `changes`, `tools.base.capture`, and
  `core` services

`changes.py` — write-path orchestration *and* its model, deliberately one
module: `plan_change()` + `ChangePlan`, `dry_run_outcomes()`,
`parse_config_transcript()` + `CONFIG_ERROR_PATTERNS`,
`capture_pre_change_backups()` (imports the capture service from
`tools.base.capture`). These are pure, independently unit-tested services, so
they stay module functions rather than class methods; `ChangePlan` stays here
because `changes.py` is its only consumer (§4.1).
`_APPLY_OPERATION`/`_CAPTURE_OPERATION` constants move here / to base capture
respectively.

Optional hardening (not required for v1 of the redesign): typed per-command
output and transcript-result models. Create `tools/netmiko/models.py` **only**
if ≥2 such models land at once (§4.1); otherwise co-locate them here too.

### 3.5 Composition root — `server.py`

The only module that knows FastMCP. Instantiates the three tool classes
(module-level "shared instances"; `CoreBase` is never instantiated — it exists
only as the shared base of `NornirBase`), registers every tool method on `mcp`, and exports nothing
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

for _tool in _TOOLS:  # bound methods: `self` is bound,
    mcp.tool(name=_tool.__name__)(_tool)  # so the schema has no self param
```

Registration happens on **bound methods after instantiation** — decorating
inside a class body would leak `self` into the tool schema.

## 4. Dependency rules (why there are no cycles)

- `core/*` imports nothing from `tools/*`, `server.py`, or `cli/`. It may import
  third-party libs only (`nornir`, `napalm`, `nornir_netmiko`, pydantic).
  (`core/tasks.py` keeps this true after the redesign — moving
  `netmiko_send_commands` out removes its only `nornir_netmiko` import.)
- `tools/napalm` → `tools/base` → `core`. `tools/netmiko` → `tools/base` →
  `core`. Sibling tool packages never import each other. `tools/base` imports
  `core.base` (`CoreBase`) like any other core module — the chain
  `CoreBase → NornirBase → (NapalmTool | NetmikoTool)` is pure inheritance,
  no upward imports.
- Shared services used by two tool classes live in `tools/base` (inheritance)
  or `core` — never in one child package. (`capture.py` is the concrete case;
  `changes.py` must not import `napalm_tool` or vice versa.)
- `server.py` imports the tool packages (`base`, `napalm`, `netmiko`) and is
  imported by `cli`/`__main__` only. Nothing under `core` or `tools` imports
  `server.py`.

### 4.1 Class-style rules — what becomes a method vs a function vs a file

- **Behavior a class applies to its own tool calls** → private methods on the
  class that owns it: plumbing shared by all three tool classes on `CoreBase`
  (helpers 1–4); CLI-gating shared by netmiko tools on `NetmikoTool`
  (helpers 5–8). Defined once, inherited everywhere — that inheritance is the
  DRY mechanism of the class design.
- **Pure, independently unit-tested services** (config capture, change
  planning, transcript parsing) and **Nornir task callables** (the
  `netmiko_send_commands` wrapper) → module-level functions. They hold no
  state and are tested in isolation; forcing them onto a class adds `self`
  ceremony for nothing. (`tools/base/capture.py`, `tools/netmiko/changes.py`,
  `netmiko_send_commands` in `tools/netmiko/tool.py`.)
- **A model lives beside its only consumer** — in the consumer's module, not a
  parallel `models.py`. Create a per-package `models.py` only when a package
  has ≥2 models or one is imported by 2+ modules. Today that means **no
  `models.py` anywhere**: `InventoryDevice` in `tools/base/tool.py`,
  `GetterInfo` in `tools/napalm/introspection.py`, `ChangePlan` in
  `tools/netmiko/changes.py`.
- **No one-file-one-function modules.** A small helper used by one module
  stays in that module. (Earlier drafts had `gating.py` and `tasks.py` under
  `tools/netmiko/` holding 85 and 30 lines respectively — both fold into
  `tools/netmiko/tool.py`.)

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
| `nornir_run_commands` | netmiko | `tools.netmiko` / `NetmikoTool` | `netmiko_send_commands` (in `tool.py`), gating |
| `nornir_backup_config` | NAPALM→netmiko | `tools.base` / `NornirBase` | `capture.capture_running_config`, storage, audit |
| `nornir_list_backups` | — | `tools.base` / `NornirBase` | core storage |
| `nornir_apply_config` | netmiko | `tools.netmiko` / `NetmikoTool` | `changes.*`, `base.capture`, netmiko plugin |
| `nornir_save_config` | netmiko | `tools.netmiko` / `NetmikoTool` | `netmiko_save_config`, capability gate |

`CoreBase` registers no tools — it owns the shared plumbing (helpers 1–4)
behind every row above.

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
| `nornir_mcp.server.run_nornir_task` (spy, envelope path) | `test_run_getter_normalizes_name_and_option_keys` | `nornir_mcp.core.base` (via `_run_task_envelope`) |
| `nornir_mcp.server.run_nornir_task` (spy, direct calls) | netmiko tool tests (`test_run_command*` / `test_apply*` / `test_save*`) | `nornir_mcp.tools.netmiko.tool` (module importing `run_nornir_task` for the direct calls in `nornir_run_command` / `nornir_run_commands` / `nornir_apply_config` / `nornir_save_config`) |
| `nornir_mcp.server.netmiko_send_commands` | `netmiko_fakes` (conftest, fake-name dispatch) | `nornir_mcp.tools.netmiko.tool` — the wrapper folds into this module |
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

- [x] **Phase 0 — baseline.** `uv run pytest`, `uv run ruff check .`,
  `uv run mypy .` all green; commit the current state as the reference point.
- [x] **Phase 1 — core kernel.** Create `core/`; move `runner.py`, `tasks.py`,
  `policy.py`, `capability.py`, `storage.py`, `audit.py`, `errors.py`,
  `responses.py`→`envelope.py` and their tests. Keep `nornir_mcp` root modules
  as thin re-export shims (`from nornir_mcp.core.xxx import *`) so server.py
  and remaining tests import unchanged. Green.
- [x] **Phase 2 — `tools/base` package.** Move `InventoryDevice` into `tool.py`
  (delete `models.py`); extract `capture.py` from `changes.py` (leave
  `changes.py` re-exporting `capture_running_config` for now); define
  `NornirBase(CoreBase)` in `tool.py` with the 4 server tools, moving bodies
  verbatim (docstrings/signatures intact); root `server.py` re-exports the
  tool names so direct-call tests still pass. Green.
- [x] **Phase 3 — `tools/napalm` package.** `NapalmTool`; move
  `introspection.py` (with `GetterInfo` folded in, delete `models.py`); move
  the `napalm_get` import out of server.py. Update the `run_nornir_task` spy
  anchor (§6.1) and re-export from server.py. Green.
- [x] **Phase 4 — `tools/netmiko` package.** `NetmikoTool` with helpers 5–8 as
  private methods and `netmiko_send_commands` folded into `tool.py`;
  `changes.py` keeps `ChangePlan`; drop `core/tasks.py`'s netmiko wrapper;
  retarget the `netmiko_fakes` fixture and the apply/save anchors (§6.1);
  `server.py` imports of netmiko plugin tasks no longer exist — verify
  nothing stale is patched. Green (full suite — this phase surfaces any
  missed anchor).
- [x] **Phase 5 — composition root & test rewrite.** Slim `server.py` to §3.5
  (registration loop, no tool re-exports); split `tests/test_server.py` into
  the §6 tree; rewrite direct calls to the `server` singletons; move the tool-
  surface pin test to `tests/test_e2e.py` (wire-level); move `main.py` →
  `cli/main.py` and update pyproject `[project.scripts]` + `__main__.py`.
  Green.
- [x] **Phase 6 — delete legacy shims; docs.** Remove root re-export shims
  (`nornir_mcp/responses.py`, `changes.py`, `models.py`, `introspection.py`,
  `main.py` stubs). Update `CLAUDE.md` (architecture, testing approach,
  netmiko_fakes targets, tool-definition location, dev commands if they
  changed) and `README.md`.
- [x] **Final gates.** `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy .`,
  `uv run vulture nornir_mcp tests --min-confidence 80`,
  `uv run pytest --cov=nornir_mcp --cov-branch`. Confirm the 12-tool surface
  test, signature/docstring tests, and e2e pass unchanged.

**Definition of done:** the four-class chain (`CoreBase` → `NornirBase` →
`NapalmTool`/`NetmikoTool`), each class package with its related files;
`core` with no upward imports; `server.py` reduced to composition;
all behavioral tests green with unchanged wire contracts; legacy flat files
gone; docs updated.

## 7.1 True-spirit reconciliation (post-implementation audit)

An audit of the completed migration against the plan's binding rules found
**two deviations** from the plan's true spirit (both confirmed test-green to
fix, since neither changes registered behavior):

- [x] **Class chain was a 3-way fan, not the mandated chain.** Phase 4
  implemented `NapalmTool(CoreBase)` / `NetmikoTool(CoreBase)`, breaking the
  §0.1/§3.2–3.4 contract `CoreBase → NornirBase → (NapalmTool |
  NetmikoTool)` and the DRY rationale that roadmap tools extending
  `NornirBase` inherit the whole chain. Fixed by swapping each class's base
  to `NornirBase` (import from `tools.base.tool`; verified acyclic —
  `tools/base` imports only `core.*` and its sibling `capture`). `_TOOLS`
  registration unchanged: inherited NornirBase methods on napalm/netmiko
  instances are never registered, so the 12-tool surface pin still holds.
- [x] **`server.nornir_*` aliases survived**, contradicting §3.5/§8/Phase 5
  ("exports nothing tool-shaped — the old module-level `server.nornir_*`
  symbols are **gone**; no module-level `server.<tool>` shims survive").
  Fixed by deleting the 12 aliases from `server.py` and rewriting the 67
  test call sites (`server.nornir_X(...)` → `server._<instance>.nornir_X(...)`,
  longest-name-first to avoid clobbering `nornir_run_commands` with
  `nornir_run_command`) to target the **shared instances** (§6.1: single
  source of truth for test identity, not fresh instances). e2e uses only
  `server.mcp` — untouched.

## 8. Risks, decisions, and open items

- **Wire schema must not drift.** Moving methods changes nothing FastMCP
  sees *if* docstrings and signatures move verbatim and registration binds
  `self`. Phase 5 includes a schema spot-check (one tool per family compared
  pre/post via `server.mcp.list_tools()`). Watch `ctx` specifically: NAPALM
  tools use a **required** `ctx: Context` (first positional), netmiko tools
  use an **optional** `ctx: Context | None = None` (last) — do **not** unify
  these while converting to methods; FastMCP treats a required vs optional
  `Context` param differently, which changes the wire schema. Include `ctx`
  in the Phase 5 pre/post schema spot-check.
- **Anchor churn is the main cost.** Every module move silently breaks
  monkeypatches that targeted the old path. §6.1 is the checklist; Phase 4 is
  where a missed anchor first shows up. Failure modes: fakes never invoked
  (tests hang or hit real task names) **or a patch binds to a name that no
  longer exists and the test passes vacuously** — Phase 4 must watch for
  false-positive green tests, not just failures.
- **Full test rewrite is intentional.** No module-level `server.<tool>` shims
  survive — the old import surface is part of what the redesign removes.
  `FROZEN_TOOL_NAMES` stays, but moves to the wire-level test file.
- **Keep classes stateless.** Instances in `server.py` are pure grouping
  objects — `CoreBase` included. State stays in `core`'s cached singletons
  and `EXECUTION_LOCK`; `CoreBase` imports them, never owns them (a second
  instance would bypass the process-wide lock/cache, breaking D7). Do not
  reintroduce per-instance Nornir state (D7).
- **`vulture` false positives.** Tool methods registered only via the
  `_TOOLS` tuple and re-exported names in `__init__.py` files may be flagged;
  treat as documented (same category as the conftest side-effect fixtures).
- **Decisions already made** (flag if you disagree): `CoreBase` as the base
  class in `core/base.py` (the envelope/selection plumbing becomes class
  behavior; the stateless services — runner cache, lock, storage, audit —
  stay module-level and are imported, not owned); the four-class chain is
  kept — `NornirBase` owns no plumbing of its own (it inherits helpers 1–4
  from `CoreBase`), but it carries the engine-agnostic server tools +
  `InventoryDevice` + `capture.py` orchestration (§3.2/§5); roadmap tool
  classes (diff/rollback/approval) extend it and inherit the whole chain,
  which is the DRY payoff of the depth; **no `models.py` files**
  — each model lives beside its only consumer (§4.1); helpers 5–8 become
  `NetmikoTool` methods and `netmiko_send_commands` folds into
  `tools/netmiko/tool.py` (no `gating.py`/`tasks.py`); `capture.py` in
  `tools/base` rather than `core` (it couples to NAPALM/netmiko plugins and is
  shared by base + netmiko); `nornir_list_getters` + introspection under
  `tools/napalm`; `responses.py` renamed `core/envelope.py`.
- **Open items for the implementer**: whether to add typed output/transcript
  models — only as a new `tools/netmiko/models.py` if ≥2 land at once (§4.1);
  final docstring rewording of moved modules; whether `test_server.py`
  splits should also carve out a `tests/core/test_base.py` for direct
  `CoreBase` plumbing tests (request id, validation envelope, selection);
  **confirm the roadmap tools (diff/rollback/approval) are committed** before
  paying the migration cost — the layered four-class chain is justified almost
  entirely by them, and a leaner two-package layout (`core` + one module per
  engine, no inheritance) delivers most of the decoupling if they are not.
