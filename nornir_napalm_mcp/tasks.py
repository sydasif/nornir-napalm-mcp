"""Shared task helpers — filtering, execution, and result normalization."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nornir_napalm_mcp.models import HostResult
from nornir_napalm_mcp.runner import NornirLike, get_nornir

if TYPE_CHECKING:
    from nornir.core.task import AggregatedResult


def _filter_devices(
    nr: NornirLike,
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
) -> NornirLike:
    """Filters Nornir inventory by name, group, or platform.

    Args:
        nr: The Nornir instance to filter.
        name: Device name or list of names to filter by.
        group: Group name to filter by.
        platform: Platform name to filter by.

    Returns:
        A filtered Nornir instance containing only matching devices.

    Raises:
        ValueError: If no devices match the provided filters.
    """
    if name:
        names = {name} if isinstance(name, str) else set(name)
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


def _result_to_dict(result: AggregatedResult) -> dict[str, HostResult]:
    """Converts a Nornir AggregatedResult into a dict of HostResult keyed by host."""
    output: dict[str, HostResult] = {}
    for host, multi_result in result.items():
        if not multi_result:
            output[host] = HostResult(ok=False, error="No tasks returned for host")
            continue
        if multi_result.failed:
            failure = multi_result[0].exception or multi_result[0].result
            output[host] = HostResult(ok=False, error=str(failure))
        else:
            output[host] = HostResult(ok=True, data=multi_result[0].result)
    return output


def run_nornir_task(
    task: Any,
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    **task_kwargs: Any,
) -> dict[str, HostResult]:
    """Run a Nornir task against filtered devices and return HostResult dict.

    Args:
        task: The Nornir task function to execute.
        name: Device name or list of names to target.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.
        **task_kwargs: Additional keyword arguments passed to the task.

    Returns:
        A dictionary mapping each device name to a HostResult.
    """
    nr: NornirLike = get_nornir()
    nr.data.reset_failed_hosts()
    nr = _filter_devices(nr, name=name, group=group, platform=platform)
    result = nr.run(task=task, **task_kwargs)
    return _result_to_dict(result)
