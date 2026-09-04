"""Nornir-NAPALM FastMCP Server — server instance and tool definitions.

The CLI entry point lives in ``main.py``.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from fastmcp import Context, FastMCP
from nornir_netmiko.tasks import (
    netmiko_save_config,
    netmiko_send_command,
    netmiko_send_config,
)

from nornir_mcp.audit import get_audit_logger
from nornir_mcp.capability import netmiko_device_type
from nornir_mcp.changes import (
    capture_pre_change_backups,
    dry_run_outcomes,
    parse_config_transcript,
    plan_change,
)
from nornir_mcp.errors import (
    BackupError,
    CommandRejectedError,
    McpError,
    UnsupportedOperationError,
    ValidationError,
)
from nornir_mcp.policy import assert_read_allowed, canonicalize
from nornir_mcp.responses import (
    HostOutcome,
    StructuredError,
    ToolEnvelope,
    maybe_truncate,
    outcome_from_mcp_error,
)
from nornir_mcp.runner import execution_lock, get_nornir
from nornir_mcp.tasks import _filter_devices, netmiko_send_commands, run_nornir_task
from nornir_mcp.tools.base.tool import NornirBase
from nornir_mcp.tools.napalm.tool import NapalmTool

mcp = FastMCP(
    name="Nornir-NAPALM Server",
    instructions="Query network devices via NAPALM. Call nornir_list_inventory first.",
)

# Phase-2 compat: re-export the 4 NornirBase tools so existing test
# references like ``server.nornir_list_inventory(...)`` still resolve.
# The actual definition lives in ``tools.base.tool``; these bound methods
# are registered on the MCP server so the wire surface is unchanged.
_nornir_base = NornirBase()
for _tool in (
    _nornir_base.nornir_list_inventory,
    _nornir_base.nornir_reload_inventory,
    _nornir_base.nornir_backup_config,
    _nornir_base.nornir_list_backups,
):
    mcp.tool(name=_tool.__name__)(_tool)

# Module-level aliases for direct-call tests (``server.nornir_*``).
nornir_list_inventory: Any = _nornir_base.nornir_list_inventory
nornir_reload_inventory: Any = _nornir_base.nornir_reload_inventory
nornir_backup_config: Any = _nornir_base.nornir_backup_config
nornir_list_backups: Any = _nornir_base.nornir_list_backups

# Phase-3 compat: re-export the 4 NapalmTool so existing test references still resolve.
_napalm_tools = NapalmTool()
for _tool in (
    _napalm_tools.nornir_get_facts,
    _napalm_tools.nornir_run_getter,
    _napalm_tools.nornir_get_config,
    _napalm_tools.nornir_list_getters,
):
    mcp.tool(name=_tool.__name__)(_tool)  # type: ignore[assignment]

nornir_get_facts: Any = _napalm_tools.nornir_get_facts
nornir_run_getter: Any = _napalm_tools.nornir_run_getter
nornir_get_config: Any = _napalm_tools.nornir_get_config
nornir_list_getters: Any = _napalm_tools.nornir_list_getters


def _request_id(ctx: Context | None) -> str:
    """Correlation id for the current request.

    Prefers ``ctx.request_id`` from the injected MCP Context. FastMCP
    raises ``RuntimeError`` when no MCP session is established (e.g. a
    direct invocation), which is treated as "not available"; in that case a
    fresh ``uuid4().hex`` is used.
    """
    try:
        request_id: str | None = ctx.request_id if ctx is not None else None
    except (RuntimeError, AttributeError):
        request_id = None
    return request_id if request_id is not None else uuid4().hex


def _run_task_envelope(
    ctx: Context,
    operation: str,
    task: Any,
    *,
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    **task_kwargs: Any,
) -> ToolEnvelope:
    """Run a Nornir task and wrap the per-host outcomes in a ToolEnvelope.

    Request-level failures — ``ValidationError`` from ``_filter_devices``
    (e.g. an explicitly empty name list) or the bare ``ValueError`` for no
    matching devices — become a request-level StructuredError on the
    envelope instead of a raised exception.
    """
    try:
        outcomes = run_nornir_task(
            task, operation=operation, name=name, group=group, platform=platform, **task_kwargs
        )
    except McpError as exc:
        return ToolEnvelope(
            operation=operation,
            request_id=_request_id(ctx),
            results={},
            error=StructuredError(
                type=exc.error_type.value,
                message=exc.message,
                host=exc.host,
                operation=operation,
                retryable=exc.retryable,
            ),
        )
    except ValueError as exc:
        return ToolEnvelope(
            operation=operation,
            request_id=_request_id(ctx),
            results={},
            error=StructuredError(
                type="validation",
                message=str(exc),
                operation=operation,
                retryable=False,
            ),
        )
    return ToolEnvelope(operation=operation, request_id=_request_id(ctx), results=outcomes)


def _validation_envelope(operation: str, request_id: str, message: str) -> ToolEnvelope:
    """A request-level validation failure envelope (empty results)."""
    return ToolEnvelope(
        operation=operation,
        request_id=request_id,
        results={},
        error=StructuredError(
            type="validation",
            message=message,
            operation=operation,
            retryable=False,
        ),
    )


def _select_targets(
    operation: str, request_id: str, name: Any, group: str | None, platform: str | None
) -> tuple[ToolEnvelope | None, Any | None]:
    """Request-level device selection.

    Returns ``(None, targets)`` on success, or ``(error_envelope, None)``
    when no devices match / the selection is explicitly empty.
    """
    try:
        nr = get_nornir()
        nr.data.reset_failed_hosts()
        targets = _filter_devices(nr, name=name, group=group, platform=platform)
    except (ValidationError, ValueError) as exc:
        message = exc.message if isinstance(exc, ValidationError) else str(exc)
        return _validation_envelope(operation, request_id, message), None
    return None, targets


def _gate_host(command: str, hostname: str, platform: str, operation: str) -> HostOutcome | None:
    """Per-host capability and policy gate for CLI commands.

    Returns ``None`` when the host may run *command*, otherwise a failed
    HostOutcome: ``unsupported_operation`` when the platform has no netmiko
    device_type mapping, ``command_rejected`` when the command is not
    READ_ONLY/SAFE_OPERATIONAL (the category is in the message).
    """
    try:
        netmiko_device_type(platform)
    except UnsupportedOperationError as exc:
        return outcome_from_mcp_error(
            UnsupportedOperationError(exc.message, host=hostname, operation=operation)
        )
    try:
        assert_read_allowed(command, platform)
    except CommandRejectedError as exc:
        return outcome_from_mcp_error(
            CommandRejectedError(exc.message, host=hostname, operation=operation)
        )
    return None


def _gate_commands(commands: list[str], platform: str) -> tuple[list[str], dict[str, str]]:
    """Gate a batch of commands for one platform.

    Returns ``(allowed, rejected)`` where *allowed* preserves the original
    order and *rejected* maps each disallowed command to its reason.
    """
    allowed: list[str] = []
    rejected: dict[str, str] = {}
    for command in commands:
        try:
            assert_read_allowed(command, platform)
        except CommandRejectedError as exc:
            rejected[command] = exc.message
            continue
        allowed.append(command)
    return allowed, rejected


def _truncated_command_outputs(raw_map: Any, commands: list[str]) -> dict[str, dict[str, object]]:
    """Truncate each command's output into the §21.1 per-command shape."""
    outputs: dict[str, dict[str, object]] = {}
    data = raw_map if isinstance(raw_map, dict) else {}
    for command in commands:
        raw = data.get(command)
        text = raw if isinstance(raw, str) else str(raw or "")
        truncated, flagged, original_size = maybe_truncate(text)
        outputs[command] = {
            "output": truncated,
            "truncated": flagged,
            "original_size": original_size,
        }
    return outputs


def _truncated_outputs(outcomes: dict[str, HostOutcome], command: str) -> dict[str, HostOutcome]:
    """Wrap successful CLI outputs in the §21.1 truncation envelope shape.

    Successful outcomes become ``data = {"command", "output",
    "truncated", "original_size"}``; failures pass through untouched.
    """
    wrapped: dict[str, HostOutcome] = {}
    for hostname, outcome in outcomes.items():
        if not outcome.success:
            wrapped[hostname] = outcome
            continue
        raw = outcome.data if isinstance(outcome.data, str) else str(outcome.data or "")
        truncated, flagged, original_size = maybe_truncate(raw)
        wrapped[hostname] = HostOutcome(
            success=True,
            data={
                "command": command,
                "output": truncated,
                "truncated": flagged,
                "original_size": original_size,
            },
        )
    return wrapped


@mcp.tool()
def nornir_run_command(
    command: str,
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    ctx: Context | None = None,
) -> ToolEnvelope:
    """Runs one read-only command on network device(s) via the CLI.

    Every command is validated **before** execution: newline/control-char
    injection is rejected at request level, and per device the command
    must classify as READ_ONLY or SAFE_OPERATIONAL — configuration,
    dangerous, blocked, or unknown commands (and unsupported platforms)
    fail for that device and nothing is sent to it. Only
    READ_ONLY/SAFE_OPERATIONAL commands are possible through this tool.
    The original command string is what reaches the device.

    Omit all filters to target every device in the inventory.

    Args:
        command: Single-line CLI command to run (e.g. 'show version').
        name: Device name or list of names to query.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.

    Returns:
        A ToolEnvelope with one HostOutcome per gated device. Successful
        outcomes carry ``data = {"command", "output", "truncated",
        "original_size"}`` per spec §21.1.
    """
    operation = "nornir_run_command"
    request_id = _request_id(ctx)

    # Request-level validation: multi-line/control-char/empty/overlong
    # commands are structurally impossible before any host is touched.
    try:
        canonicalize(command)
    except ValidationError as exc:
        return _validation_envelope(operation, request_id, exc.message)

    # Request-level device selection: no matches / empty selection.
    error, targets = _select_targets(operation, request_id, name, group, platform)
    if error is not None:
        return error
    assert targets is not None

    # Per-host capability + policy gate; only passing hosts execute.
    outcomes: dict[str, HostOutcome] = {}
    allowed: list[str] = []
    for host in targets.inventory.hosts.values():
        hostname = host.name
        gate = _gate_host(command, hostname, str(host.platform), operation)
        if gate is not None:
            outcomes[hostname] = gate
        else:
            allowed.append(hostname)

    if allowed:
        executed = run_nornir_task(
            netmiko_send_command,
            operation=operation,
            name=allowed,
            command_string=command,
        )
        outcomes.update(_truncated_outputs(executed, command))

    return ToolEnvelope(operation=operation, request_id=request_id, results=outcomes)


@mcp.tool()
def nornir_run_commands(
    commands: list[str],
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    ctx: Context | None = None,
) -> ToolEnvelope:
    """Runs a batch of read-only commands on network device(s) via the CLI.

    Each command is validated individually **before** execution against the
    same policy as nornir_run_command (only READ_ONLY and
    SAFE_OPERATIONAL commands can run). A disallowed command fails THAT
    COMMAND ONLY — it is never sent — while the remaining allowed
    commands still run. Any command containing newlines or control
    characters fails the whole request before anything is sent.

    Omit all filters to target every device in the inventory.

    Args:
        commands: Single-line CLI commands, each validated individually.
        name: Device name or list of names to query.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.

    Returns:
        A ToolEnvelope with one HostOutcome per gated device. Successful
        outcomes carry ``data = {"commands": {cmd: {"output",
        "truncated", "original_size"}}, "rejected": {cmd: {"error":
        reason}}}``; a device whose commands were all rejected reports
        ``success=False`` with a ``command_rejected`` error.
    """
    operation = "nornir_run_commands"
    request_id = _request_id(ctx)

    # Request-level validation of the whole batch: nothing malformed is
    # ever sent, and an empty batch is an error.
    if not commands:
        return _validation_envelope(operation, request_id, "commands list is empty")
    for command in commands:
        try:
            canonicalize(command)
        except ValidationError as exc:
            return _validation_envelope(operation, request_id, exc.message)

    # Request-level device selection.
    error, targets = _select_targets(operation, request_id, name, group, platform)
    if error is not None:
        return error
    assert targets is not None

    # Group target hosts by platform; capability-gate each host first.
    outcomes: dict[str, HostOutcome] = {}
    platform_hosts: dict[str, list[str]] = {}
    for host in targets.inventory.hosts.values():
        hostname = host.name
        platform = str(host.platform)
        try:
            netmiko_device_type(platform)
        except UnsupportedOperationError as exc:
            outcomes[hostname] = outcome_from_mcp_error(
                UnsupportedOperationError(exc.message, host=hostname, operation=operation)
            )
            continue
        platform_hosts.setdefault(platform, []).append(hostname)

    # Per platform: gate every command, execute the allowed ones only.
    for platform, hostnames in platform_hosts.items():
        allowed, rejected = _gate_commands(commands, platform)
        rejected_map = {cmd: {"error": reason} for cmd, reason in rejected.items()}
        if not allowed:
            for hostname in hostnames:
                outcomes[hostname] = HostOutcome(
                    success=False,
                    data={"commands": {}, "rejected": rejected_map},
                    error=StructuredError(
                        type="command_rejected",
                        message=(
                            "All requested commands were rejected by the read-only "
                            "policy for this device."
                        ),
                        host=hostname,
                        operation=operation,
                        retryable=False,
                    ),
                )
            continue

        executed = run_nornir_task(
            netmiko_send_commands,
            operation=operation,
            name=hostnames,
            commands=allowed,
        )
        for hostname in hostnames:
            run_outcome = executed.get(hostname)
            if run_outcome is None or not run_outcome.success:
                run_error = (
                    run_outcome.error
                    if run_outcome is not None and run_outcome.error is not None
                    else StructuredError(
                        type="connection",
                        message="command batch execution failed",
                        host=hostname,
                        operation=operation,
                        retryable=True,
                    )
                )
                outcomes[hostname] = HostOutcome(
                    success=False,
                    data={"commands": {}, "rejected": rejected_map},
                    error=run_error,
                )
                continue
            cmd_outputs = _truncated_command_outputs(run_outcome.data, allowed)
            outcomes[hostname] = HostOutcome(
                success=True,
                data={"commands": cmd_outputs, "rejected": rejected_map},
            )

    return ToolEnvelope(operation=operation, request_id=request_id, results=outcomes)


@mcp.tool()
def nornir_apply_config(
    config: list[str],
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    dry_run: bool = True,
    ctx: Context | None = None,
) -> ToolEnvelope:
    """Applies configuration lines to network device(s) (spec §8).

    Flow (normative): filter hosts -> plan_change (per-host policy +
    capability gating) -> if dry_run, return the §16.1 ``planned_commands_only``
    plan and touch nothing -> capture pre-change backups (fail-closed:
    any backup failure aborts with a ``backup`` request-level error and
    devices stay untouched) -> ``netmiko_send_config`` per planned host ->
    parse each transcript heuristically -> audit (hash only, never the
    config text) -> envelope.

    ``dry_run`` defaults to True — planning and backup are the only side
    effects, never device writes. There is deliberately **no** ``backup``
    parameter (decision D5): pre-change backups are always captured and
    retained; they are the rollback substrate for this tool.

    Transcript parsing is honest-heuristic (D10): netmiko returns a
    transcript, not per-line pass/fail. Known error patterns mark a host's
    applied/device state as unknown — a host with a detected error is
    NEVER reported as success (spec §8.3); ``device_state`` is
    "unknown" unless a read-back happened (it doesn't, in v1). Partial
    applies are reported honestly per host.

    Args:
        config: Configuration lines to apply, in order (validated per
            host: DANGEROUS/BLOCKED lines are rejected and never sent).
        name: Device name or list of names to target.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.
        dry_run: If True (default), only plan — no backups, no device
            writes.

    Returns:
        A ToolEnvelope with one HostOutcome per device. Successful
        outcomes carry ``data = {"applied", "failed_at",
        "device_state", "change_id", "backup_id"}`` (plus
        ``"transcript"`` when error patterns were detected).
    """
    operation = "nornir_apply_config"
    request_id = _request_id(ctx)

    if not config:
        return _validation_envelope(operation, request_id, "config is empty — nothing to apply")

    # Request-level device selection: no matches / empty selection.
    error, targets = _select_targets(operation, request_id, name, group, platform)
    if error is not None:
        return error
    assert targets is not None

    hosts = {host.name: str(host.platform) for host in targets.inventory.hosts.values()}
    plan = plan_change(hosts, config)
    joined_hash = hashlib.sha256("\n".join(config).encode()).hexdigest()
    audit = get_audit_logger()

    # Dry run: §16.1 planned-commands-only outcomes; NO device calls.
    if dry_run:
        outcomes = dry_run_outcomes(plan, request_id)
        audit.record(
            operation,
            request_id,
            change_id=plan.change_id,
            hosts=list(hosts),
            result="dry_run",
            details={"sha256": joined_hash},
        )
        return ToolEnvelope(operation=operation, request_id=request_id, results=outcomes)

    # Real apply: pre-change backups fail-closed, then execute per host.
    # One lock acquisition spans backup + apply so no other request can
    # interleave between them (RLock: nested run_nornir_task is fine).
    with execution_lock():
        try:
            records = capture_pre_change_backups(plan)
        except BackupError as exc:
            audit.record(
                operation,
                request_id,
                change_id=plan.change_id,
                hosts=list(hosts),
                result="blocked",
                details={"sha256": joined_hash},
            )
            return ToolEnvelope(
                operation=operation,
                request_id=request_id,
                results={},
                error=StructuredError(
                    type=exc.error_type.value,
                    message=exc.message,
                    operation=operation,
                    retryable=exc.retryable,
                ),
            )
        planned = [
            host
            for host in hosts
            if host not in plan.violations and host not in plan.capability_errors
        ]
        if planned:
            executed = run_nornir_task(
                netmiko_send_config,
                operation=operation,
                name=planned,
                config_commands=config,
            )

    # Per-host outcomes: violations/capability failures flow through from
    # the plan; planned hosts get transcript-parsed outcomes.
    outcomes = dry_run_outcomes(plan, request_id)
    for hostname in planned:
        outcome = executed.get(hostname)
        if outcome is None or not outcome.success:
            outcomes[hostname] = outcome or HostOutcome(
                success=False,
                error=StructuredError(
                    type="connection",
                    message=f"config apply for '{hostname}' returned no result",
                    host=hostname,
                    operation=operation,
                    retryable=True,
                ),
            )
            continue
        transcript = outcome.data if isinstance(outcome.data, str) else str(outcome.data or "")
        parsed = parse_config_transcript(transcript, hosts[hostname], config)
        backup_id = records[hostname].backup_id if hostname in records else None
        data: dict[str, object] = {
            **parsed,
            "change_id": plan.change_id,
            "backup_id": backup_id,
        }
        if parsed["applied"] is None:
            # §8.3: never report a host with a detected error as success.
            outcomes[hostname] = HostOutcome(
                success=False,
                data=data,
                error=StructuredError(
                    type="configuration",
                    message=(
                        f"device '{hostname}' transcript reports configuration errors — "
                        "applied/device_state unknown (see transcript)"
                    ),
                    host=hostname,
                    operation=operation,
                    retryable=False,
                ),
            )
        else:
            outcomes[hostname] = HostOutcome(success=True, data=data)

    successes = sum(1 for outcome in outcomes.values() if outcome.success)
    if successes == len(outcomes):
        result = "success"
    elif successes == 0:
        result = "failed"
    else:
        result = "partial"
    audit.record(
        operation,
        request_id,
        change_id=plan.change_id,
        hosts=list(outcomes),
        result=result,
        details={"sha256": joined_hash},
    )
    return ToolEnvelope(operation=operation, request_id=request_id, results=outcomes)


@mcp.tool()
def nornir_save_config(
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    ctx: Context | None = None,
) -> ToolEnvelope:
    """Persists the running configuration to startup/NVRAM (spec §11).

    Explicit-only operation: ``nornir_save_config`` is **never** invoked
    implicitly — ``nornir_apply_config`` does not call it and never will.
    Saving is deliberately a separate, audited, human-visible step. This
    writes NVRAM on the devices (``write memory`` / ``copy
    running-config startup-config`` per platform) and is audit-logged.

    Omit all filters to target every device in the inventory.

    Args:
        name: Device name or list of names to save.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.

    Returns:
        A ToolEnvelope with one HostOutcome per gated device. Unsupported
        platforms fail per host with an ``unsupported_operation`` error;
        successful outcomes carry the plugin's save result text.
    """
    operation = "nornir_save_config"
    request_id = _request_id(ctx)

    error, targets = _select_targets(operation, request_id, name, group, platform)
    if error is not None:
        return error
    assert targets is not None

    # Per-host capability gate: only netmiko-capable platforms can save.
    outcomes: dict[str, HostOutcome] = {}
    capable: list[str] = []
    for host in targets.inventory.hosts.values():
        hostname = host.name
        try:
            netmiko_device_type(str(host.platform))
        except UnsupportedOperationError as exc:
            outcomes[hostname] = outcome_from_mcp_error(
                UnsupportedOperationError(exc.message, host=hostname, operation=operation)
            )
        else:
            capable.append(hostname)

    if capable:
        outcomes.update(
            run_nornir_task(
                netmiko_save_config,
                operation=operation,
                name=capable,
            )
        )

    successes = sum(1 for outcome in outcomes.values() if outcome.success)
    if not outcomes:
        result = "no_hosts"
    elif successes == len(outcomes):
        result = "success"
    elif successes == 0:
        result = "failed"
    else:
        result = "partial"
    get_audit_logger().record(
        operation,
        request_id,
        hosts=list(outcomes),
        result=result,
    )
    return ToolEnvelope(operation=operation, request_id=request_id, results=outcomes)
