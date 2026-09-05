"""NetmikoTool — netmiko CLI and write-path tools."""

from __future__ import annotations

import hashlib
from typing import Any

from fastmcp import Context
from nornir.core.task import Result
from nornir_netmiko.tasks import (
    netmiko_save_config,
    netmiko_send_command,
    netmiko_send_config,
)

from nornir_mcp.core.audit import get_audit_logger
from nornir_mcp.core.capability import netmiko_device_type
from nornir_mcp.core.envelope import (
    HostOutcome,
    StructuredError,
    ToolEnvelope,
    outcome_from_mcp_error,
)
from nornir_mcp.core.errors import (
    BackupError,
    CommandRejectedError,
    UnsupportedOperationError,
    ValidationError,
)
from nornir_mcp.core.policy import assert_read_allowed, canonicalize
from nornir_mcp.core.runner import execution_lock
from nornir_mcp.core.tasks import run_nornir_task
from nornir_mcp.tools.base.tool import NornirBase
from nornir_mcp.tools.netmiko.changes import (
    capture_pre_change_backups,
    dry_run_outcomes,
    parse_config_transcript,
    plan_change,
)


def netmiko_send_commands(task: Any, commands: list[str] | None = None, **kwargs: Any) -> Any:
    """Run a batch of read-only commands over a netmiko connection.

    nornir-netmiko 1.0.x no longer ships a batch plugin, so this in-house
    task replicates the classic plugin semantics: each command is sent via
    ``netmiko_send_command`` and collected into ``{hostname: {command:
    output}}``. Tests replace the module-level name with the canned
    ``fake_netmiko_send_commands`` (see conftest), which records exactly
    which commands were sent.

    Args:
        task: The Nornir task for the target host.
        commands: The commands to run, in order.

    Returns:
        A Nornir ``Result`` whose ``result`` maps the hostname to a
        per-command output dict.
    """
    outputs: dict[str, str] = {}
    for command in commands or []:
        result = netmiko_send_command(task, command_string=command, **kwargs)
        outputs[command] = result.result
    return Result(host=task.host, result=outputs)


class NetmikoTool(NornirBase):
    """Netmiko-family tools: CLI command runs and write-path ops."""

    def nornir_run_command(
        self,
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
        request_id = self._request_id(ctx)

        try:
            canonicalize(command)
        except ValidationError as exc:
            return self._validation_envelope(operation, request_id, exc.message)

        error, targets = self._select_targets(operation, request_id, name, group, platform)
        if error is not None:
            return error
        assert targets is not None

        outcomes: dict[str, HostOutcome] = {}
        allowed: list[str] = []
        for host in targets.inventory.hosts.values():
            hostname = host.name
            gate = self._gate_host(command, hostname, str(host.platform), operation)
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
            outcomes.update(self._truncated_outputs(executed, command))

        return ToolEnvelope(operation=operation, request_id=request_id, results=outcomes)

    def nornir_run_commands(
        self,
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
        request_id = self._request_id(ctx)

        if not commands:
            return self._validation_envelope(operation, request_id, "commands list is empty")
        for command in commands:
            try:
                canonicalize(command)
            except ValidationError as exc:
                return self._validation_envelope(operation, request_id, exc.message)

        error, targets = self._select_targets(operation, request_id, name, group, platform)
        if error is not None:
            return error
        assert targets is not None

        outcomes: dict[str, HostOutcome] = {}
        platform_hosts: dict[str, list[str]] = {}
        for host in targets.inventory.hosts.values():
            hostname = host.name
            plat = str(host.platform)
            try:
                netmiko_device_type(plat)
            except UnsupportedOperationError as exc:
                outcomes[hostname] = outcome_from_mcp_error(
                    UnsupportedOperationError(exc.message, host=hostname, operation=operation)
                )
                continue
            platform_hosts.setdefault(plat, []).append(hostname)

        for plat, hostnames in platform_hosts.items():
            allowed, rejected = self._gate_commands(commands, plat)
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
                cmd_outputs = self._truncated_command_outputs(run_outcome.data, allowed)
                outcomes[hostname] = HostOutcome(
                    success=True,
                    data={"commands": cmd_outputs, "rejected": rejected_map},
                )

        return ToolEnvelope(operation=operation, request_id=request_id, results=outcomes)

    def nornir_apply_config(
        self,
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
        request_id = self._request_id(ctx)

        if not config:
            return self._validation_envelope(
                operation, request_id, "config is empty — nothing to apply"
            )

        error, targets = self._select_targets(operation, request_id, name, group, platform)
        if error is not None:
            return error
        assert targets is not None

        hosts = {host.name: str(host.platform) for host in targets.inventory.hosts.values()}
        plan = plan_change(hosts, config)
        joined_hash = hashlib.sha256("\n".join(config).encode()).hexdigest()
        audit = get_audit_logger()

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
            executed: dict[str, HostOutcome] = {}
            if planned:
                executed = run_nornir_task(
                    netmiko_send_config,
                    operation=operation,
                    name=planned,
                    config_commands=config,
                )

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

    def nornir_save_config(
        self,
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
        request_id = self._request_id(ctx)

        error, targets = self._select_targets(operation, request_id, name, group, platform)
        if error is not None:
            return error
        assert targets is not None

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

    # -----------------------------------------------------------------------
    # Private helpers (moved from server.py; reused by both run_command and
    # run_commands; these are *not* MCP tools.)
    # -----------------------------------------------------------------------

    def _gate_host(
        self, command: str, hostname: str, platform: str, operation: str
    ) -> HostOutcome | None:
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

    def _gate_commands(
        self, commands: list[str], platform: str
    ) -> tuple[list[str], dict[str, str]]:
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

    def _truncated_command_outputs(
        self, raw_map: Any, commands: list[str]
    ) -> dict[str, dict[str, object]]:
        """Truncate each command's output into the §21.1 per-command shape."""
        outputs: dict[str, dict[str, object]] = {}
        data = raw_map if isinstance(raw_map, dict) else {}
        for command in commands:
            raw = data.get(command)
            text = raw if isinstance(raw, str) else str(raw or "")
            truncated, flagged, original_size = self._maybe_truncate(text)
            outputs[command] = {
                "output": truncated,
                "truncated": flagged,
                "original_size": original_size,
            }
        return outputs

    def _truncated_outputs(
        self, outcomes: dict[str, HostOutcome], command: str
    ) -> dict[str, HostOutcome]:
        """Wrap successful CLI outputs in the §21.1 truncation envelope shape.

        Successful outcomes become ``data = {"command", "output",
        "truncated", "original_size"}``; failures pass through untouched.
        """
        from nornir_mcp.core.envelope import maybe_truncate

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

    @staticmethod
    def _maybe_truncate(text: str) -> tuple[str, bool, int]:
        """Truncate *text* to the configured byte budget."""
        from nornir_mcp.core.envelope import maybe_truncate

        return maybe_truncate(text)
