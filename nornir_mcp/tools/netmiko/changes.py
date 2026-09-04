"""Change orchestration for the write path (spec §8, §9).

:func:`plan_change` is the pure planning step: per host, lines are gated
by :func:`validate_config_lines` and the platform checked for ruleset/netmiko
capability, producing a :class:`ChangePlan` with no device I/O.

:func:`capture_pre_change_backups` implements the §8.2 fail-closed rule:
if the pre-change capture or backup of **any** planned host fails, a
:class:`BackupError` is raised and the device is never touched. Backups
saved before the failure are harmless extra state — they are immutable and
cost nothing to retain.

:func:`dry_run_outcomes` turns a plan into §16.1-labeled outcomes
(``dry_run_mode: planned_commands_only``) without touching devices.

:func:`parse_config_transcript` is the D10 honest heuristic: netmiko
returns a transcript, not per-line pass/fail, so known error patterns are
searched and, when found, the apply is reported as unknown with a
truncated transcript excerpt. A host with a detected error is NEVER
reported as success (spec §8.3).
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from nornir_mcp.capability import netmiko_device_type
from nornir_mcp.core.envelope import HostOutcome, StructuredError, maybe_truncate
from nornir_mcp.core.policy import PolicyViolation, validate_config_lines
from nornir_mcp.core.storage import BackupRecord, BackupStore, get_backup_store
from nornir_mcp.errors import BackupError, UnsupportedOperationError
from nornir_mcp.tools.base.capture import capture_running_config

# Operation label attached to errors for traceability.
_APPLY_OPERATION = "apply_config"

# Known device error patterns per platform (D10 honest heuristic). These are
# the patterns we *know* indicate a failed config line; absence of a match
# means "no error detected", never "verified good".
CONFIG_ERROR_PATTERNS: dict[str, list[str]] = {
    "ios": [r"% Invalid input", r"% Incomplete command", r"% Ambiguous command"],
    "eos": [r"Error:", r"% Invalid", r"incomplete"],
}


class ChangePlan(BaseModel):
    """The validated plan for one configuration change (spec §8).

    Pure data: produced by :func:`plan_change` with no device I/O.
    ``hosts`` maps device name to platform; ``violations`` and
    ``capability_errors`` record per-host gate failures (empty for hosts
    that passed planning).
    """

    change_id: str
    hosts: dict[str, str]
    lines: list[str]
    violations: dict[str, list[PolicyViolation]]
    capability_errors: dict[str, str]


def plan_change(hosts: dict[str, str], lines: list[str]) -> ChangePlan:
    """Gate a candidate change per host without touching any device.

    For each host: the platform must have netmiko/ruleset capability
    (else it lands in ``capability_errors`` and is not line-validated),
    then the lines are run through :func:`validate_config_lines` and any
    DANGEROUS/BLOCKED/shape violations are recorded per host.

    Args:
        hosts: Mapping of device name to platform.
        lines: The candidate configuration lines, in order.

    Returns:
        A :class:`ChangePlan` carrying the generated ``chg-<12 hex>``
        change id and the per-host gate results.
    """
    violations: dict[str, list[PolicyViolation]] = {}
    capability_errors: dict[str, str] = {}
    for host, platform in hosts.items():
        try:
            netmiko_device_type(platform)
        except UnsupportedOperationError as exc:
            capability_errors[host] = str(exc)
            continue
        line_violations = validate_config_lines(lines, platform)
        if line_violations:
            violations[host] = line_violations
    return ChangePlan(
        change_id=f"chg-{uuid4().hex[:12]}",
        hosts=dict(hosts),
        lines=list(lines),
        violations=violations,
        capability_errors=capability_errors,
    )


_CaptureFn = Any


def capture_pre_change_backups(
    plan: ChangePlan,
    capture: _CaptureFn = capture_running_config,
    store: BackupStore | None = None,
) -> dict[str, BackupRecord]:
    """Capture and store pre-change backups for every planned host (§8.2).

    Only hosts that passed planning (no violations, no capability errors)
    are captured. For each: the running config is captured (via *capture*)
    and saved with ``trigger="pre_change"`` and the plan's ``change_id``.
    The *change_id* doubles as the capture's request correlation id.

    **Fail-closed**: any capture or save failure raises
    :class:`BackupError` naming the failing host — apply must abort and
    the device must NOT be touched. Backups already saved before the
    failure are harmless extra state: they are immutable and retained for
    audit.

    Args:
        plan: The validated change plan.
        capture: Injectable capture callable (defaults to
            :func:`capture_running_config`).
        store: Injectable backup store (defaults to the process-wide
            store from ``NORNIR_MCP_BACKUP_DIR``).

    Returns:
        Mapping of host name to its stored BackupRecord.

    Raises:
        BackupError: If any host's capture or save fails (spec §8.2
            fail-closed).
    """
    store = store if store is not None else get_backup_store()
    planned = [
        host
        for host in plan.hosts
        if host not in plan.violations and host not in plan.capability_errors
    ]
    records: dict[str, BackupRecord] = {}
    for host in planned:
        platform = plan.hosts[host]
        try:
            content = capture(host, platform, plan.change_id)
            records[host] = store.save(
                host, content, trigger="pre_change", change_id=plan.change_id
            )
        except BackupError:
            raise
        except Exception as exc:
            raise BackupError(
                f"pre-change backup failed for '{host}' (change {plan.change_id}): "
                f"{exc} — apply aborted, the device was NOT touched (spec §8.2)",
                host=host,
                operation=_APPLY_OPERATION,
            ) from exc
    return records


def parse_config_transcript(transcript: str, platform: str, lines: list[str]) -> dict[str, Any]:
    """Heuristically parse a netmiko config-apply transcript (D10).

    Netmiko returns a transcript, not per-line pass/fail, so this is an
    honest heuristic — not a parser: known error patterns are searched
    (case-insensitive) and, when found, the apply is reported as unknown
    with a truncated transcript excerpt. A host with a detected error is
    NEVER reported as success (spec §8.3). When no pattern matches, the
    lines are reported as applied with ``device_state`` explicitly marked
    "unknown (no error patterns detected)" — the device state is
    ``"unknown"`` unless a read-back happened (it doesn't, in v1).

    Args:
        transcript: The raw netmiko transcript for one device.
        platform: NAPALM platform name (pattern set lookup).
        lines: The config lines that were sent (reported on success).

    Returns:
        ``{"applied": lines | None, "failed_at": None,
        "device_state": str, "transcript": excerpt (errors only)}``.
    """
    patterns = CONFIG_ERROR_PATTERNS.get(platform, [])
    for pattern in patterns:
        if re.search(pattern, transcript, re.IGNORECASE):
            excerpt, _, _ = maybe_truncate(transcript)
            return {
                "applied": None,
                "failed_at": None,
                "device_state": "unknown",
                "transcript": excerpt,
            }
    return {
        "applied": lines,
        "failed_at": None,
        "device_state": "unknown (no error patterns detected)",
    }


def dry_run_outcomes(plan: ChangePlan, request_id: str) -> dict[str, HostOutcome]:
    """Build per-host outcomes for the dry run of *plan* (§16.1).

    Capability-failed hosts get ``unsupported_operation`` outcomes;
    hosts with policy violations get ``command_rejected`` outcomes; hosts
    that passed planning get a success outcome whose data carries the
    mandatory §16.1 label ``dry_run_mode: "planned_commands_only"``
    plus the planned lines and ``policy_result: "pass"``. No device is
    touched.

    Args:
        plan: The validated change plan.
        request_id: Request correlation id, included in error messages.

    Returns:
        Mapping of host name to HostOutcome.
    """
    outcomes: dict[str, HostOutcome] = {}
    for host in plan.hosts:
        if host in plan.capability_errors:
            outcomes[host] = HostOutcome(
                success=False,
                data=None,
                error=StructuredError(
                    type="unsupported_operation",
                    message=plan.capability_errors[host],
                    host=host,
                    operation=_APPLY_OPERATION,
                    retryable=False,
                ),
            )
            continue
        if host in plan.violations:
            reasons = "; ".join(v.reason for v in plan.violations[host])
            outcomes[host] = HostOutcome(
                success=False,
                data=None,
                error=StructuredError(
                    type="command_rejected",
                    message=(
                        f"configuration change for '{host}' rejected by policy "
                        f"({len(plan.violations[host])} violation(s)): {reasons} "
                        f"(request {request_id})"
                    ),
                    host=host,
                    operation=_APPLY_OPERATION,
                    retryable=False,
                ),
            )
            continue
        outcomes[host] = HostOutcome(
            success=True,
            data={
                "dry_run_mode": "planned_commands_only",
                "commands": plan.lines,
                "policy_result": "pass",
            },
        )
    return outcomes
