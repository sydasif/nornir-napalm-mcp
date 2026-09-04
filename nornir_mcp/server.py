"""Nornir-NAPALM FastMCP Server — server instance and tool definitions.

The CLI entry point lives in ``main.py``.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastmcp import Context, FastMCP

from nornir_mcp.capability import netmiko_device_type
from nornir_mcp.errors import (
    CommandRejectedError,
    McpError,
    UnsupportedOperationError,
    ValidationError,
)
from nornir_mcp.policy import assert_read_allowed
from nornir_mcp.responses import (
    HostOutcome,
    StructuredError,
    ToolEnvelope,
    maybe_truncate,
    outcome_from_mcp_error,
)
from nornir_mcp.runner import get_nornir
from nornir_mcp.tasks import _filter_devices, run_nornir_task
from nornir_mcp.tools.base.tool import NornirBase
from nornir_mcp.tools.napalm.tool import NapalmTool
from nornir_mcp.tools.netmiko.tool import NetmikoTool

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
for _tool in (  # type: ignore[assignment]
    _napalm_tools.nornir_get_facts,
    _napalm_tools.nornir_run_getter,
    _napalm_tools.nornir_get_config,
    _napalm_tools.nornir_list_getters,
):
    mcp.tool(name=_tool.__name__)(_tool)

nornir_get_facts: Any = _napalm_tools.nornir_get_facts
nornir_run_getter: Any = _napalm_tools.nornir_run_getter
nornir_get_config: Any = _napalm_tools.nornir_get_config
nornir_list_getters: Any = _napalm_tools.nornir_list_getters

# Phase-4 compat: re-export the 4 NetmikoTool so existing test references still resolve.
_netmiko_tools = NetmikoTool()
for _tool in (  # type: ignore[assignment]
    _netmiko_tools.nornir_run_command,
    _netmiko_tools.nornir_run_commands,
    _netmiko_tools.nornir_apply_config,
    _netmiko_tools.nornir_save_config,
):
    mcp.tool(name=_tool.__name__)(_tool)

nornir_run_command: Any = _netmiko_tools.nornir_run_command
nornir_run_commands: Any = _netmiko_tools.nornir_run_commands
nornir_apply_config: Any = _netmiko_tools.nornir_apply_config
nornir_save_config: Any = _netmiko_tools.nornir_save_config


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
