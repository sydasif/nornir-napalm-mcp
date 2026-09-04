# Network Automation MCP Server
## Developer-Ready Technical Specification

### 1. Project Overview

Build an MCP server that allows AI assistants such as Claude, Cursor, and other MCP-compatible clients to safely inspect, operate, validate, and configure network devices through a unified interface.

The system shall use:

* **MCP** for the AI-facing interface
* **Nornir** for inventory, orchestration, concurrency, and task execution
* **NAPALM** for structured device information and supported network abstractions
* **Netmiko** for raw CLI commands and CLI configuration
* Optional **TextFSM / Genie / custom parsers** for converting CLI output into structured data
* Local or Git-backed storage for configuration backups and audit history

The design must support both read-only network intelligence and controlled write operations.

---

# 2. Primary Goals

The system must allow an AI assistant to:

1. Discover network device state.
2. Run safe read-only CLI commands.
3. Execute configuration commands.
4. Back up configuration before changes.
5. Preview configuration changes.
6. Apply configuration with safety checks.
7. Save configurations.
8. Validate post-change state.
9. Roll back using stored configuration when supported.
10. Operate against multiple devices through Nornir.
11. Return compact, structured results suitable for LLM consumption.
12. Produce sufficient audit information to determine what happened, when, where, and why.

---

# 3. Non-Goals

The first implementation should not attempt to become a full network management platform.

Do not initially include:

* Full web UI
* Distributed job scheduling
* Multi-tenant SaaS
* Automatic topology discovery
* Automatic remediation without policy checks
* Unrestricted arbitrary shell execution on the MCP host
* Unrestricted device configuration
* Vendor-specific support for every networking platform

The architecture must allow these capabilities to be added later.

---

# 4. High-Level Architecture

```text
                         AI Assistant
                    Claude / Cursor / MCP
                              |
                              | MCP
                              v
                    +---------------------+
                    |     MCP Server      |
                    |                     |
                    | Tool Definitions    |
                    | Input Validation    |
                    | Safety Policies     |
                    | Approval Handling   |
                    +----------+----------+
                               |
                         Service Layer
                               |
             +-----------------+-----------------+
             |                                   |
             v                                   v
      Structured Operations                CLI Operations
             |                                   |
          NAPALM                             Netmiko
             |                                   |
             +-----------------+-----------------+
                               |
                             Nornir
                               |
                        Nornir Inventory
                               |
                               v
                       Network Devices
```

Configuration-related operations must pass through:

```text
MCP Request
    ↓
Schema Validation
    ↓
Authorization / Policy Check
    ↓
Dangerous Command Check
    ↓
Capability Check (does this host/driver support the requested operation?)
    ↓
Backup (required — failure here blocks the apply, see Section 9)
    ↓
Dry Run / Diff (required on first execution of a change; see Section 16)
    ↓
Nornir Task
    ↓
NAPALM / Netmiko
    ↓
Post-Change Validation
    ↓
Structured MCP Response
    ↓
Audit Record
```

Backup and dry-run are drawn as mandatory, non-optional stages in this pipeline for any write operation — see Sections 8, 9, and 16 for why they cannot be deferred to a later milestone.

---

# 5. Core Design Principles

### 5.1 Nornir is the execution orchestrator

All device operations should go through Nornir wherever practical.

The MCP layer must not contain device-specific connection loops such as:

```python
for device in devices:
    connect(device)
```

Instead:

```text
MCP → service → Nornir task → driver
```

This preserves concurrency, inventory handling, grouping, filtering, and future driver expansion.

### 5.2 NAPALM and Netmiko have different responsibilities

NAPALM should be preferred for:

* Structured facts
* Interfaces
* IP information
* BGP
* ARP
* LLDP
* Environment
* Configuration retrieval
* Other supported getters

Netmiko should be preferred for:

* Arbitrary read-only CLI commands
* Vendor-specific show commands
* Configuration commands not covered by NAPALM
* Operational commands such as ping/traceroute when CLI execution is appropriate

### 5.3 Read and write operations must be separated

Read operations must never implicitly modify device state.

Write operations must use dedicated MCP tools and additional safety controls.

**This separation must be enforced by the classifier, not just by tool naming.** `network_run_command` and `network_run_commands` (the read-only CLI tools) must reject any command the policy engine classifies as `CONFIGURATION`, `DANGEROUS`, or `BLOCKED` (Section 13) before execution — not merely rely on the AI choosing the correct tool. A command that would change device state must be structurally impossible to run through a "read-only" tool.

### 5.4 Return structured data whenever possible

Avoid returning huge raw device outputs when structured data can be produced.

Large outputs should be:

* Parsed
* Truncated where appropriate
* Summarized
* Stored separately when necessary

Truncation is not a per-tool nicety — see Section 21.1 for the cross-cutting contract every tool must honor.

---

# 6. MCP Tool API

## 6.1 Discovery / Facts

### `network_get_facts`

Retrieve device identity and basic platform information.

Input:

```json
{
  "hosts": ["R1", "R2"]
}
```

Result:

```json
{
  "R1": {
    "success": true,
    "data": {
      "hostname": "R1",
      "vendor": "Cisco",
      "model": "...",
      "os_version": "...",
      "serial_number": "..."
    }
  }
}
```

---

## 6.2 Interface State

### `network_get_interfaces`

Retrieve interface status and operational information.

### `network_get_interfaces_ip`

Retrieve interface IP addressing.

---

## 6.3 Routing / Neighbor Information

### `network_get_bgp_neighbors`

### `network_get_arp_table`

### `network_get_lldp_neighbors`

### `network_get_environment`

These tools should map directly to supported NAPALM getters.

Unsupported getters must return a structured capability error rather than crashing the entire request.

---

# 7. CLI Tooling

## 7.1 `network_run_command`

Execute one read-only CLI command.

Input:

```json
{
  "hosts": ["R1"],
  "command": "show ip interface brief"
}
```

The server must validate that the command is permitted. Per Section 5.3, this validation must reject any command classified as `CONFIGURATION`, `DANGEROUS`, `BLOCKED`, or `UNKNOWN` — this tool has no code path capable of applying configuration.

Example result:

```json
{
  "R1": {
    "success": true,
    "command": "show ip interface brief",
    "output": "..."
  }
}
```

---

## 7.2 `network_run_commands`

Run several read-only commands.

Input:

```json
{
  "hosts": ["R1", "R2"],
  "commands": [
    "show version",
    "show ip interface brief",
    "show ip route"
  ]
}
```

The server should execute efficiently through Nornir and return:

```json
{
  "R1": {
    "success": true,
    "commands": {
      "show version": "...",
      "show ip interface brief": "...",
      "show ip route": "..."
    }
  }
}
```

Every command in the batch is validated individually against the same policy as `network_run_command`; one disallowed command in a batch must fail that command only, not silently execute it or abort the whole batch (unless policy is configured to fail closed on any violation — this behavior must be explicit and tested, see Section 33).

Command output should have configurable maximum size (see Section 21.1).

---

# 8. Configuration Operations

## 8.1 `network_apply_config`

Apply configuration commands to one or more devices.

Input:

```json
{
  "hosts": ["SW1"],
  "config": [
    "interface GigabitEthernet1/0/10",
    "description Connected-to-Server",
    "switchport mode access",
    "switchport access vlan 100"
  ],
  "backup": true,
  "dry_run": false
}
```

`backup` and `dry_run` are not optional add-ons scheduled for a later milestone — see Section 8.2. They are part of this tool's contract from its very first shipped version.

The tool must:

1. Validate request schema.
2. Validate commands against policy.
3. Verify target hosts.
4. Check driver/platform capability for the requested operation (Section 28) — refuse with a structured capability error rather than attempting and failing partway through.
5. Create a configuration backup. This step is required, not conditional on a `backup` flag defaulting to true — see Section 8.2 for the fail-closed rule.
6. Execute configuration through Netmiko/Nornir.
7. Capture command results.
8. Run post-change validation when configured.
9. Return a per-host result.

### 8.2 Backup is a hard precondition, not an optional flag

`network_apply_config`'s `backup: bool` parameter controls whether the resulting backup is *retained beyond this call's audit trail* — it must not control whether a backup is *taken*. A pre-change backup is always captured internally before any write, because it is also the mechanism `network_rollback_config` depends on. If the backup step itself fails (storage unavailable, device read fails, etc.), the apply must not proceed: fail closed, return a `BackupError`, and do not touch the device. This failure mode is a named test case in Section 31.1.

### 8.3 Partial-apply behavior

Netmiko sends configuration line-by-line (or block-by-block, depending on driver); a batch can fail partway through. Nornir/NAPALM do not guarantee transactional semantics on platforms that lack a commit/confirm model (see Section 12).

`network_apply_config` must therefore report, per host:

* `applied`: the commands that were sent and acknowledged before failure (if any)
* `failed_at`: the command that failed, if applicable
* `device_state`: `"unknown"` unless a post-failure read-back was performed to confirm actual device state

A host that fails partway through configuration must never be reported as `"success": true`, and the response must make clear that the device may be in a partially-changed state rather than either its original or its fully-intended state.

---

# 9. Configuration Backup

## `network_backup_config`

Retrieve the running configuration and store it. This is also invoked internally (not just as a standalone tool) as the mandatory pre-change step described in Section 8.2.

Recommended storage:

```text
backups/
└── R1/
    ├── 2026-09-04T07-00-00Z.cfg
    ├── 2026-09-04T08-30-00Z.cfg
    └── metadata.json
```

Metadata should contain:

```json
{
  "host": "R1",
  "timestamp": "2026-09-04T08:30:00Z",
  "source": "network_get_config",
  "trigger": "pre_change",
  "change_id": "chg-123456"
}
```

Backups should be immutable once written.

A failed backup must raise a `BackupError` and propagate as a blocking failure to any caller relying on it as a precondition (Section 8.2) rather than being swallowed or logged-and-continued.

---

# 10. Configuration Diff

## `network_config_diff`

Compare the current configuration with a proposed configuration or previously stored configuration.

Output should use a standard diff format:

```diff
 interface GigabitEthernet1/0/10
- description OLD-SERVER
+ description NEW-SERVER
```

The diff result should be generated before applying a change whenever a dry-run workflow is requested.

### 10.1 Diff fidelity varies by driver

Where NAPALM's `compare_config`/`config_merge`/`config_replace` capability is available for a platform (Section 28), the diff is device-computed and authoritative. Where only Netmiko is available (no NAPALM driver, or the platform doesn't support config comparison), there is no device-side diff primitive — `network_config_diff` in that case can only report "commands that will be sent," not a computed before/after difference. The response must identify which mode produced the result (`"diff_source": "device_computed"` vs `"diff_source": "planned_commands_only"`) so callers don't treat both as equally trustworthy.

---

# 11. Save Configuration

## `network_save_config`

Persist running configuration to startup configuration.

This must be a separate operation from `network_apply_config`.

The server must not silently save configuration unless explicitly requested or required by policy.

---

# 12. Rollback

## `network_rollback_config`

Restore a previously stored configuration.

Input:

```json
{
  "host": "R1",
  "backup_id": "R1-2026-09-04T07:00:00Z"
}
```

Rollback must:

1. Verify backup exists.
2. Verify target host.
3. Check driver/platform capability for the restore method being used (Section 28).
4. Require appropriate authorization (Section 15.1).
5. Produce a change ID.
6. Apply rollback configuration.
7. Validate result.
8. Record audit information.

Rollback behavior is vendor/driver dependent and must not assume transactional semantics on platforms that do not provide them.

---

# 13. Safety Layer

The safety layer is a mandatory architectural component.

## 13.1 Command Categories

Commands should be classified into:

```text
READ_ONLY
SAFE_OPERATIONAL
CONFIGURATION
DANGEROUS
BLOCKED
UNKNOWN
```

Example (IOS-style syntax):

```text
show version              → READ_ONLY
show ip route             → READ_ONLY
ping 8.8.8.8              → SAFE_OPERATIONAL
interface Gi1/0/1         → CONFIGURATION
reload                    → DANGEROUS
write erase               → BLOCKED
erase startup-config      → BLOCKED
```

Unknown commands should not automatically be considered safe.

## 13.2 Classification is vendor-specific — it must not silently default to IOS rules

The examples above hold for Cisco IOS-style syntax. They do not generalize: JunOS uses `set`/`edit`/`delete` for configuration and `request system reboot` in place of `reload`; NX-OS and other platforms have their own dangerous-command surfaces. Since per-vendor support is explicitly out of scope for v1 (Section 3), the classifier must be keyed per-platform, and **any command on a platform without a defined ruleset must classify as `UNKNOWN` and be rejected** — never fall through to IOS rules by default. Adding a new platform means adding its ruleset, not assuming the existing one applies.

---

# 14. Command Policy

Policy should be externalized into YAML rather than hard-coded whenever practical.

Example:

```yaml
blocked:
  - reload
  - write erase
  - erase startup-config
  - format
allowed_read_prefixes:
  - show
  - ping
  - traceroute
require_confirmation:
  - configure terminal
  - reload
```

The policy engine must normalize whitespace and command casing before matching.

It should detect dangerous forms such as:

```text
reload
reload in 5
reload /confirm
```

rather than relying only on exact string matches.

### 14.1 Abbreviation and prefix matching

Cisco-style CLIs accept unambiguous abbreviations — `wr e` for `write erase`, `conf t` for `configure terminal`, `rel` for `reload`. A blocklist keyed on full literal strings will not catch these. Normalization must expand recognized abbreviations to their canonical form (or match on the shortest-valid-prefix the target platform accepts) before running blocklist/dangerous-form matching, per platform ruleset (Section 13.2). This expansion table is platform-specific and must be tested with real abbreviation forms, not just full commands (Section 33).

---

# 15. Approval Model

Write operations should support:

```text
approval_required = true
```

A safe lifecycle is:

```text
REQUEST
  ↓
VALIDATE
  ↓
PREVIEW
  ↓
APPROVE
  ↓
EXECUTE
  ↓
VALIDATE
```

The implementation should keep approval logic separate from the underlying device execution service.

### 15.1 Identity: who is approving, and how does the server know?

Sections 12, 21, and 25 all assume a "who is asking" concept (`require appropriate authorization`, an audit `"user"` field, an `APPROVE` actor) — but MCP transports do not automatically carry a human identity distinct from the AI session. This must be resolved explicitly, per transport (Section 37), before approval or audit logic is built:

* **Local/stdio transport**: identity is realistically the OS user running the MCP host process. This is a weak guarantee (anyone with process access can approve) and should be documented as such rather than presented as real authorization.
* **HTTP transport**: identity should come from an explicit auth mechanism (API key, OAuth token, mTLS client cert — whichever the deployment uses) passed with the request, not inferred from the AI's own session.

Whichever model is chosen, `audit_service` and the approval workflow must reference the same identity source — an audit record whose `"user"` field doesn't match anything the approval step actually checked is not a real control.

### 15.2 Approval state must persist across calls

Between `PREVIEW` and `APPROVE` there are at least two separate MCP calls (the AI proposes, then something/someone approves later). The proposed change — target hosts, commands, generated diff, an approval id, and a TTL after which it expires — must be persisted somewhere addressable by that id. This needs a home in the code structure (Section 29) and a service (`approval_service.py`) rather than being implied by the lifecycle diagram alone.

---

# 16. Dry Run

Configuration tools should support:

```json
{
  "dry_run": true
}
```

Dry run must not modify the device.

The response should explain:

```text
target devices
planned commands
policy result
configuration diff if available
```

### 16.1 Dry-run fidelity also varies by driver

Same caveat as Section 10.1: a NAPALM-backed dry run can return a real device-computed diff. A Netmiko-only dry run has no such primitive — it can only echo back "these are the commands that would be sent," without device-side confirmation that they'd apply cleanly. The response must label which kind of dry run was performed (`"dry_run_mode": "device_computed" | "planned_commands_only"`) so this distinction isn't lost on the caller.

---

# 17. Post-Change Validation

A configuration change should optionally trigger validation.

Examples:

```text
Interface status
BGP neighbor state
OSPF neighbors
VLAN membership
IP address
Reachability
Configuration presence
```

Example:

```json
{
  "validation": [
    {
      "type": "interface",
      "interface": "GigabitEthernet1/0/10",
      "expected": "up"
    }
  ]
}
```

The result should clearly separate:

```text
configuration_success
validation_success
```

A successful configuration command with failed validation must not be reported simply as "success".

---

# 18. Dedicated Operational Tools

Provide higher-level tools where they improve safety and usability.

## `network_ping`

Rather than requiring the AI to build a raw CLI command.

## `network_traceroute`

Likewise, exposed as a dedicated operation where the driver supports it.

These tools can later support vendor-independent implementations.

---

# 19. Device Selection

Nornir inventory should remain the authoritative source for device identity.

Tools should support:

```text
hosts
groups
filters
```

Examples:

```json
{
  "hosts": ["R1", "R2"]
}
```

or:

```json
{
  "groups": ["routers"]
}
```

The implementation must prevent arbitrary user input from being interpreted as a local filesystem path, shell command, or inventory source.

---

# 20. Data Model

Use typed schemas for all MCP inputs and outputs.

Recommended approach:

```text
Pydantic models
```

Example:

```python
class CommandRequest(BaseModel):
    hosts: list[str]
    command: str
    timeout: int = 30
```

Configuration:

```python
class ConfigRequest(BaseModel):
    hosts: list[str]
    config: list[str]
    backup: bool = True     # controls retention, not whether a backup is taken — see Section 8.2
    dry_run: bool = True
    save: bool = False
```

---

# 21. Standard Result Model

Every operation should return a predictable structure.

```json
{
  "success": false,
  "operation": "network_run_command",
  "request_id": "req-123",
  "results": {
    "R1": {
      "success": true,
      "data": {}
    },
    "R2": {
      "success": false,
      "error": {
        "type": "connection_error",
        "message": "Connection timed out"
      }
    }
  }
}
```

One failed device must not hide successful results from other devices.

**Invariant:** top-level `"success"` is `true` if and only if every per-host result's `"success"` is `true`. A single failing host among many always yields top-level `"success": false`, even though individual successful results remain visible under `"results"`. Section 34's multi-device failure testing depends on this invariant holding exactly, not approximately.

## 21.1 Truncation contract

Any field capable of holding large output (raw CLI output, full running configuration, bulk getter results) must honor a configurable maximum size, applied consistently across every tool rather than per-tool as an afterthought. When truncation occurs, the response must say so explicitly (e.g. `"truncated": true, "original_size": N`) rather than silently clipping. This applies with particular force at the concurrency targets in Section 35 (50–100 devices), where untruncated full-config retrieval across a whole inventory could produce a response far larger than any LLM context window can use productively.

---

# 22. Error Handling

Errors must be categorized.

Recommended classes:

```text
ValidationError
InventoryError
ConnectionError
AuthenticationError
TimeoutError
CommandRejectedError
UnsupportedOperationError
ParseError
ConfigurationError
BackupError
ValidationFailure
RollbackError
InternalError
```

Each error must provide:

```text
error type
human-readable message
target device
operation
retryable flag
```

Example:

```json
{
  "type": "connection_error",
  "message": "SSH connection timed out",
  "host": "R2",
  "retryable": true
}
```

Do not expose passwords, private keys, tokens, or other credentials in MCP responses or logs.

---

# 23. Retry Strategy

Retries must be conservative.

Recommended retry candidates:

* Connection timeout
* Temporary SSH/network failure
* Transient transport errors

Do not automatically retry:

* Authentication failure
* Invalid configuration
* Command policy rejection
* Syntax errors
* Unsupported operation

Configuration operations should not blindly repeat if there is a possibility the first attempt succeeded but the response was lost.

### 23.1 Mechanism for "verify before retry"

Stating the principle above is not enough on its own — it needs a mechanism. Before retrying any configuration operation whose outcome is uncertain (response lost, connection dropped mid-apply), the retry path must re-run the relevant Section 17 post-change validation (or, at minimum, re-read the current config/state) to determine whether the original attempt already succeeded, rather than resending the same commands unconditionally. Where feasible, callers should also be able to supply an idempotency key per change so a retried request can be recognized as a duplicate of one already in flight or completed.

---

# 24. Timeouts

Timeouts must exist at multiple levels:

```text
Connection timeout
Command timeout
Task timeout
Overall request timeout
```

Avoid indefinite MCP calls.

A device that hangs must not block all other devices.

---

# 25. Logging and Auditing

Use two separate concepts.

### Application logs

For debugging:

```text
INFO
WARNING
ERROR
DEBUG
```

### Audit logs

For operational accountability:

```json
{
  "change_id": "chg-123",
  "request_id": "req-456",
  "timestamp": "...",
  "user": "...",
  "operation": "network_apply_config",
  "hosts": ["SW1"],
  "commands": ["..."],
  "result": "success"
}
```

The `"user"` field must be populated from the identity source defined in Section 15.1 — not left as a placeholder the schema implies but nothing actually fills in per transport.

Secrets must never be included.

For sensitive configuration, consider storing a hash/reference rather than duplicating data in audit records.

---

# 26. Configuration Storage

The initial implementation may use:

```text
local filesystem
```

with a future path toward:

```text
Git
GitLab
Gitea
S3-compatible object storage
database
```

Use a storage abstraction:

```python
class BackupStore:
    save()
    get()
    list()
    delete()
```

The service layer should not depend directly on filesystem calls.

---

# 27. CLI Parsing

Raw CLI output should remain available, but structured parsing should be preferred when a parser exists.

Recommended processing:

```text
CLI Output
    ↓
Parser
    ↓
Structured Data
    ↓
MCP Response
```

Potential parser backends:

```text
TextFSM
Genie / pyATS
Custom parser
No parser → raw output
```

The MCP response should identify whether output was parsed.

Example:

```json
{
  "raw_output": "...",
  "parsed": {
    "interfaces": []
  },
  "parser": "genie"
}
```

---

# 28. Capability Detection

The system should be able to determine what a device supports.

Example:

```json
{
  "host": "R1",
  "capabilities": {
    "napalm": true,
    "get_bgp_neighbors": true,
    "config_replace": false,
    "config_merge": true,
    "cli": true
  }
}
```

This prevents the AI from attempting unsupported operations.

**Capability detection must be a required precondition inside `network_apply_config` and `network_rollback_config` (Sections 8 and 12), not just a standalone tool the AI might forget to call first.** Both write-path tools should check capability internally before executing and return a structured capability error if the requested operation isn't supported for that host/platform, exactly as they already do for policy and host-existence checks.

---

# 29. Suggested Code Structure

```text
network-mcp/
│
├── pyproject.toml
├── README.md
├── .env.example
│
├── src/
│   └── network_mcp/
│       ├── server.py
│       │
│       ├── tools/
│       │   ├── discovery.py
│       │   ├── napalm.py
│       │   ├── cli.py
│       │   ├── config.py
│       │   ├── validation.py
│       │   └── backup.py
│       │
│       ├── services/
│       │   ├── nornir_service.py
│       │   ├── execution_service.py
│       │   ├── safety_service.py
│       │   ├── backup_service.py
│       │   ├── validation_service.py
│       │   ├── approval_service.py
│       │   └── audit_service.py
│       │
│       ├── drivers/
│       │   ├── napalm_driver.py
│       │   ├── netmiko_driver.py
│       │   └── interface.py
│       │
│       ├── parsers/
│       │   ├── textfsm_parser.py
│       │   ├── genie_parser.py
│       │   └── registry.py
│       │
│       ├── models/
│       │   ├── requests.py
│       │   ├── responses.py
│       │   ├── errors.py
│       │   └── devices.py
│       │
│       ├── policy/
│       │   ├── command_policy.py
│       │   └── policies.yaml
│       │
│       └── storage/
│           ├── interface.py
│           └── filesystem.py
│
├── inventory/
│   ├── hosts.yaml
│   ├── groups.yaml
│   └── defaults.yaml
│
├── backups/
│
├── approvals/
│
├── audit/
│
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/
```

`approval_service.py` and the `approvals/` directory support the pending-approval persistence described in Section 15.2.

---

# 30. Separation of Responsibilities

The implementation must preserve these boundaries:

```text
MCP tools
    ↓
Request/response models
Services
    ↓
Business logic
Safety
    ↓
Policy enforcement
Nornir
    ↓
Execution orchestration
Drivers
    ↓
NAPALM / Netmiko
Storage
    ↓
Backups / audit
```

MCP tool functions should remain thin.

Avoid putting connection logic, policy logic, backup logic, and parsing logic directly inside MCP tool functions.

---

# 31. Testing Strategy

Testing must cover four levels.

## 31.1 Unit Tests

Test independently:

* Command classification, per platform ruleset (not just IOS)
* Abbreviation/prefix expansion before classification (`wr e`, `conf t`, `rel`)
* Unrecognized-platform commands classify as `UNKNOWN` and are rejected, not passed through under a default ruleset
* Dangerous command detection
* Input schema validation
* Host selection
* Result normalization, including the aggregate `success` invariant (Section 21)
* Error classification
* Backup naming
* **Backup failure blocks apply** (Section 8.2) — apply must not proceed and must return `BackupError`
* Configuration diff generation, and correct `diff_source` labeling (device-computed vs planned-commands-only)
* Parser selection
* Timeout handling
* Truncation is applied and flagged when output exceeds the configured maximum (Section 21.1)

Example:

```text
test_reload_is_blocked()
test_show_command_is_allowed()
test_unknown_command_is_rejected()
test_config_requires_write_permission()
test_apply_config_without_successful_backup_is_rejected()
test_unrecognized_platform_command_is_unknown_not_ios_default()
test_abbreviated_write_erase_is_blocked()
```

---

## 31.2 Mocked Driver Tests

Mock NAPALM and Netmiko.

Test:

```text
successful command
failed command
timeout
authentication failure
unsupported getter
partial multi-device failure
configuration failure
partial configuration failure mid-batch (Section 8.3)
capability check rejects unsupported operation before any device write
```

These tests must not require real network equipment.

---

## 31.3 Lab Integration Tests

Use a real network lab such as:

```text
EVE-NG
Containerlab
virtual Cisco IOS
Cisco NX-OS virtual image
Linux networking devices
```

Test full execution through:

```text
MCP
→ Nornir
→ driver
→ device
```

Test:

1. Device discovery
2. NAPALM getters
3. Show commands
4. Configuration commands
5. Backup
6. Save
7. Post-change validation
8. Rollback

---

# 32. End-to-End MCP Tests

Run against a real MCP client.

Example workflow:

```text
AI:
"Check BGP neighbors on R1 and R2."
        ↓
network_get_bgp_neighbors
        ↓
AI:
"R2 has one down BGP peer."
        ↓
network_run_command
show ip bgp summary
        ↓
AI:
"Would you like me to change anything?"
```

A write scenario:

```text
AI
 ↓
Inspect device
 ↓
Propose change
 ↓
Generate diff
 ↓
Approval
 ↓
Backup
 ↓
Apply
 ↓
Validate
 ↓
Audit
```

---

# 33. Safety Test Matrix

The following must have automated tests.

| Input                       | Expected                          |
| ---------------------------- | ---------------------------------- |
| `show version`               | Allow                              |
| `show ip route`               | Allow                              |
| `ping 8.8.8.8`                 | Allow according to policy          |
| `interface Gi1/0/1`           | Configuration workflow             |
| `reload`                       | Block/approval                     |
| `write erase`                  | Block                              |
| `wr e` (abbreviated)           | Block — same as full form          |
| `erase startup-config`         | Block                              |
| Empty command                 | Reject                             |
| Excessively long command      | Reject                             |
| Shell injection attempt       | Reject                             |
| Unknown command                | Reject or require explicit policy  |
| Command on unrecognized platform | Reject as `UNKNOWN`, never default to IOS rules |
| Config-classified command via `network_run_command` | Reject — read-only tool cannot execute it |

---

# 34. Multi-Device Failure Testing

Test combinations such as:

```text
R1 → success
R2 → timeout
R3 → authentication failure
R4 → configuration rejected
```

Expected response:

```text
R1: success
R2: timeout / retryable
R3: authentication failure / non-retryable
R4: configuration failure
```

The overall request should preserve all individual device results, and top-level `success` must be `false` per the Section 21 invariant even though R1 individually succeeded.

---

# 35. Performance Requirements

Nornir should execute independent device tasks concurrently.

The server should support at least:

```text
10 devices
50 devices
100 devices
```

as progressively tested targets.

Measure:

```text
average request latency
per-device execution time
concurrency
memory consumption
failure rate
```

Avoid creating excessive threads or connections.

Concurrency should be configurable.

Example:

```yaml
nornir:
  num_workers: 20
```

---

# 36. Security Requirements

Credentials must come from secure configuration mechanisms.

Preferred order:

```text
environment / secret store
    ↓
Nornir inventory references
    ↓
runtime credential injection
```

Do not:

* Return passwords through MCP
* Log passwords
* Put secrets in Git
* Include private keys in tool responses
* Accept arbitrary local commands from the AI
* Allow the AI to directly execute commands on the MCP host

---

# 37. Transport Modes

The server should be designed to support:

### Local MCP

For:

```text
Claude Desktop
local development
local agents
```

Identity for approval/audit purposes (Section 15.1) is realistically the OS user running the MCP host process in this mode — a weak guarantee, and one that should be documented as such rather than treated as real access control.

### HTTP MCP

For:

```text
remote clients
internal services
future multi-user deployments
```

Identity for approval/audit purposes in this mode must come from an explicit auth mechanism (API key, OAuth token, mTLS client certificate) carried with the request — not inferred from the AI's own session or omitted.

The network execution layer should remain independent from the MCP transport.

---

# 38. Future Extensions

The architecture should leave extension points for:

```text
Scrapli
async execution
Git configuration history
RBAC
job queues
scheduled tasks
compliance engine
network topology
intent-based automation
LLM-assisted CLI parsing
configuration templates
Jinja2
Napalm-Validate
OpenConfig
RESTCONF
NETCONF
gNMI
```

These must not complicate the initial implementation.

---

# 39. Recommended Implementation Order

## Milestone 1 — Existing Foundation

```text
Nornir
+
NAPALM
+
MCP
```

Maintain existing information-gathering tools.

## Milestone 2 — CLI

Implement:

```text
network_run_command
network_run_commands
```

using Nornir + Netmiko.

Add command validation immediately, including the read-only enforcement rule in Section 5.3 (these tools must refuse anything classified above `READ_ONLY`/`SAFE_OPERATIONAL`) and the abbreviation-matching rule in Section 14.1.

## Milestone 3 — Configuration, with its safety net included

Implement:

```text
network_apply_config
network_save_config
```

**Backup and dry-run ship as part of this milestone, not after it.** The original ordering deferred backup/dry-run to Milestone 4, which means the first working version of `network_apply_config` would be able to write to live devices with only a command blocklist standing between the AI and the device — no undo path. Section 8.2's fail-closed backup precondition and Section 16's dry-run must both be functional before `network_apply_config` is considered done, even in its first version. Approval and rollback can still wait for Milestone 4/5 — those are workflow refinements, not the last line of defense.

## Milestone 4 — Safety

Implement:

```text
command policy
approval
dangerous-command blocking
```

(Dry run and backup have moved to Milestone 3 — see above. Resolve the identity model (Section 15.1) here, before approval logic depends on it.)

## Milestone 5 — Configuration Lifecycle

Implement:

```text
network_backup_config (as a standalone callable tool, distinct from the internal pre-change backup already required since Milestone 3)
network_config_diff
network_rollback_config
```

## Milestone 6 — Validation

Implement:

```text
network_validate_interfaces
network_validate_bgp
network_validate_reachability
network_validate_config
```

## Milestone 7 — Parsing

Add:

```text
TextFSM
Genie
custom parser registry
```

## Milestone 8 — Enterprise Features

Add:

```text
Git
audit backend
RBAC
HTTP transport
job execution
Scrapli
```

---

# 40. Definition of Done

The project is ready for its first production-oriented release when all of the following are true:

### Read Operations

* NAPALM getters work through MCP.
* CLI show commands work through MCP.
* Multiple devices execute concurrently.
* Partial failures are represented correctly.
* Results are structured and predictable.

### Write Operations

* Configuration commands use a dedicated tool.
* Dangerous commands are blocked, including abbreviated and platform-specific forms.
* Configuration backups happen automatically before every write and block the write on failure.
* Dry run works, and labels which fidelity mode produced its result.
* Diff is available, and labels which fidelity mode produced its result.
* Save is explicit.
* Post-change validation works.
* Rollback is available where technically supported.
* Partial-apply state is reported accurately, never as unqualified success.

### Security

* Credentials are never exposed in MCP output.
* Audit logs contain no secrets.
* Arbitrary local shell execution is impossible.
* Command policy is enforced server-side, per platform, with no default-to-IOS fallback for unrecognized platforms.
* Identity used for approval and audit is defined and populated per transport (Section 15.1).

### Reliability

* Timeouts are enforced.
* Retry behavior is controlled, and verifies state before retrying uncertain-outcome configuration operations.
* Device failures do not crash the whole request.
* Exceptions are converted into structured MCP errors.
* Aggregate `success` follows the all-hosts-succeeded invariant (Section 21) with no exceptions.

### Testing

* Unit tests pass.
* Mocked driver tests pass.
* Lab integration tests pass.
* MCP end-to-end tests pass.
* Safety tests cover dangerous commands, their abbreviations, and unrecognized-platform fallback behavior.

---

# 41. Target End-State

The finished platform should support this workflow:

```text
                     AI Assistant
                          |
                          v
                    MCP Interface
                          |
                          v
                 +----------------+
                 | Safety / Policy|
                 +-------+--------+
                         |
             +-----------+-----------+
             |                       |
             v                       v
        Read Operations        Write Operations
             |                       |
         NAPALM                   Backup
             |                    ↓
             |                  Diff
             |                    ↓
             |                 Approval
             |                    ↓
             |                Netmiko
             |                    ↓
             |                Validation
             |                    ↓
             +---------+----------+
                       |
                     Nornir
                       |
             +---------+---------+
             |         |         |
             v         v         v
            R1        R2        SW1
```

The key architectural decision is to **treat MCP as the control interface, not the automation engine**. Nornir remains responsible for orchestration, NAPALM handles structured network state, and Netmiko handles CLI operations. Safety, backup, validation, and audit logic sit above the drivers so that future transports such as Scrapli, NETCONF, RESTCONF, or gNMI can be added without redesigning the MCP interface.

---

# 42. Immediate Development Target

The first implementation milestone after the current NAPALM MCP should be:

```text
Nornir + Netmiko + MCP
```

with exactly these initial tools:

```text
network_run_command
network_run_commands
network_apply_config
network_save_config
```

plus the mandatory supporting services:

```text
command_policy
result_normalizer
error_handler
audit_logger
backup_service        # required from day one — see Section 8.2, not deferred
```

**`network_apply_config`'s first shipped version must already include the mandatory pre-change backup (Section 8.2) and dry-run support (Section 16).** These are not part of the "add later" list below — see Milestone 3 in Section 39 for why. What genuinely can wait:

```text
diff (beyond planned-commands-only)
approval workflow
rollback
post-change validation
```

This keeps the first code change small while creating the correct foundation for the larger platform — without leaving a window where the AI can write to a live device with no way to undo it.
