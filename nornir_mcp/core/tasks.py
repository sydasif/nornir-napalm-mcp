"""Shared task helpers — filtering, execution, and outcome normalization.

``netmiko_send_commands`` lives here temporarily; it folds into
``tools/netmiko/tool.py`` in Phase 4 of the redesign (which also removes
``core``'s only ``nornir_netmiko`` import).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nornir.core.task import Result
from nornir_netmiko.tasks import netmiko_send_command

from nornir_mcp.core.envelope import HostOutcome, StructuredError, outcome_from_mcp_error
from nornir_mcp.core.errors import DeviceConnectionError, ValidationError
from nornir_mcp.core.runner import NornirLike, execution_lock, get_nornir

if TYPE_CHECKING:
    from nornir.core.task import AggregatedResult

__all__ = [
    "_filter_devices",
    "_results_to_outcomes",
    "run_nornir_task",
    "netmiko_send_commands",
]


def _filter_devices(
    nr: NornirLike,
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
) -> NornirLike:
    """Filters Nornir inventory by name, group, or platform.

    Omit all filters to target every device in the inventory.

    Args:
        nr: The Nornir instance to filter.
        name: Device name or list of names to filter by.
        group: Group name to filter by.
        platform: Platform name to filter by.

    Returns:
        A filtered Nornir instance containing only matching devices.

    Raises:
        ValidationError: If *name* is an explicitly empty selection
            (``[]`` or ``""``) — an empty selection is an error, not
            "all devices".
        ValueError: If no devices match the provided filters.
    """
    if name is not None:
        names = {name} if isinstance(name, str) and name else set(name)
        if not names:
            raise ValidationError("explicitly empty device list provided")
        nr = nr.filter(filter_func=lambda h: h.name in names)
    if group:
        nr = nr.filter(filter_func=lambda h: any(g.name == group for g in h.groups))
    if platform:
        nr = nr.filter(platform=platform)

    if not nr.inventory.hosts:
        raise ValueError(
            "No devices match the provided filters. "
            "Call nornir_list_inventory to see available devices."
        )

    return nr


def _results_to_outcomes(result: AggregatedResult, operation: str) -> dict[str, HostOutcome]:
    """Converts a Nornir AggregatedResult into per-host HostOutcomes.

    Args:
        result: The Nornir AggregatedResult to normalize.
        operation: The tool/operation name, attached to any error payloads.

    Returns:
        A dict mapping each host name to its HostOutcome. A host that
        failed with an exception becomes a retryable ``connection`` error
        (transient transport failures are the most common cause); a host
        that failed without an exception becomes a non-retryable
        ``internal`` error carrying the result's message.
    """
    output: dict[str, HostOutcome] = {}
    for host, multi_result in result.items():
        if not multi_result:
            output[host] = HostOutcome(
                success=False,
                data=None,
                error=StructuredError(
                    type="internal",
                    message="No tasks returned for host",
                    host=host,
                    operation=operation,
                    retryable=False,
                ),
            )
            continue
        if multi_result.failed:
            failure = multi_result[0].exception or multi_result[0].result
            if isinstance(failure, Exception):
                output[host] = outcome_from_mcp_error(
                    DeviceConnectionError(str(failure), host=host, operation=operation)
                )
            else:
                output[host] = HostOutcome(
                    success=False,
                    data=None,
                    error=StructuredError(
                        type="internal",
                        message=str(failure),
                        host=host,
                        operation=operation,
                        retryable=False,
                    ),
                )
            continue
        task_result = multi_result[0]
        output[host] = HostOutcome(success=True, data=task_result.result)
    return output


def run_nornir_task(
    task: Any,
    operation: str,
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    **task_kwargs: Any,
) -> dict[str, HostOutcome]:
    """Run a Nornir task against filtered devices and return host outcomes.

    Args:
        task: The Nornir task function to execute.
        operation: The tool/operation name, used in error construction.
        name: Device name or list of names to target.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.

    Other Parameters:
        **task_kwargs: Additional keyword arguments passed to the task.

    Returns:
        A dictionary mapping each device name to its HostOutcome.

    Raises:
        ValueError: If no devices match the provided filters.
    """
    with execution_lock():
        nr: NornirLike = get_nornir()
        nr.data.reset_failed_hosts()
        nr = _filter_devices(nr, name=name, group=group, platform=platform)
        result = nr.run(task=task, **task_kwargs)
        return _results_to_outcomes(result, operation)


def netmiko_send_commands(task: Any, commands: list[str] | None = None, **kwargs: Any) -> Any:
    """Run a batch of read-only commands over a netmiko connection.

    nornir-netmiko 1.0.x no longer ships a batch plugin, so this in-house
    task replicates the classic plugin semantics: each command is sent via
    ``netmiko_send_command`` and collected into ``{hostname: {command:
    output}}``. Tests replace the server-level name with the canned
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
    return Result(host=task.host, result={task.host.name: outputs})
