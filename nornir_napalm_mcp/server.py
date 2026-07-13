"""Nornir-NAPALM FastMCP Server — tool definitions only.

The FastMCP instance and CLI entry point live in ``main.py``.
"""

import logging
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from nornir_napalm.plugins.tasks import napalm_cli, napalm_get

from nornir_napalm_mcp.models import GetterInfo, HostResult, InventoryDevice
from nornir_napalm_mcp.runner import get_nornir, reset_nornir

if TYPE_CHECKING:
    from nornir.core import Nornir
    from nornir.core.task import AggregatedResult  # noqa: F401 - used in type hints

log = logging.getLogger("nornir-napalm-mcp")

mcp = FastMCP(
    name="Nornir-NAPALM Server",
    instructions="Query network devices via NAPALM. Call nornir_list_inventory first.",
)


def _filter_devices(
    nr: "Nornir",
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
) -> "Nornir":
    """Filters Nornir inventory by name, group, or platform.

    At least one filter parameter should be provided. If no devices match
    the given filters, a ValueError is raised with available device names.

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
    # Capture the pre-filter host names up front so the error message below
    # doesn't need a second, separate fetch of the global Nornir instance.
    all_host_names = list(nr.inventory.hosts)

    if name:
        names = [name] if isinstance(name, str) else name
        nr = nr.filter(filter_func=lambda h: h.name in names)
    if group:
        nr = nr.filter(filter_func=lambda h: group in [g.name for g in h.groups])
    if platform:
        nr = nr.filter(platform=platform)

    if not nr.inventory.hosts:
        available = ", ".join(sorted(all_host_names)) or "(none)"
        raise ValueError(
            f"No devices match filters (name={name}, group={group}, platform={platform}). "
            f"Available devices: {available}. Call nornir_list_inventory."
        )

    return nr


def _run_nornir_task(
    task: Any,
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    **task_kwargs: Any,
) -> dict[str, HostResult]:
    """Run a Nornir task against filtered devices and return HostResult dict.

    Args:
        task: The Nornir task function to execute (e.g., napalm_get, napalm_cli).
        name: Device name or list of names to target.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.
        **task_kwargs: Additional keyword arguments passed to the task.

    Returns:
        A dictionary mapping each device name to a HostResult.
    """
    nr = _filter_devices(get_nornir(), name=name, group=group, platform=platform)
    result = nr.run(task=task, **task_kwargs)
    return _result_to_dict(result)


def _result_to_dict(result: "AggregatedResult") -> dict[str, HostResult]:
    """Converts a Nornir AggregatedResult into a dict of HostResult keyed by host.

    Per-host task failures are surfaced via an explicit ``ok=False`` /
    ``error=...`` entry rather than silently returning the underlying
    exception object as if it were normal task data.

    Args:
        result: The AggregatedResult returned by ``nr.run(...)``.

    Returns:
        A dictionary mapping each host name to a HostResult: ``ok=True``
        with ``data`` populated on success, or ``ok=False`` with ``error``
        populated if the task failed for that host.
    """
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


@mcp.tool()
def nornir_list_inventory() -> list[InventoryDevice]:
    """Lists all devices in the Nornir inventory.

    Returns:
        A sorted list of InventoryDevice objects, each containing
        the device name, hostname, platform, and group membership.
    """
    nr = get_nornir()
    return [
        InventoryDevice(
            name=host.name,
            hostname=str(host.hostname),
            platform=str(host.platform),
            groups=[g.name for g in host.groups],
        )
        for host in sorted(nr.inventory.hosts.values(), key=lambda h: h.name)
    ]


@mcp.tool()
def nornir_get_facts(
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Retrieves system facts from network device(s) via NAPALM.

    Fetches device information such as hostname, vendor, model,
    OS version, and serial number.

    Args:
        name: Device name or list of names to query.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.

    Returns:
        A dictionary mapping each device name to a HostResult. On success,
        ``data`` contains the facts dictionary (hostname, vendor, model,
        os_version, serial_number). On failure, ``ok`` is False and
        ``error`` describes what went wrong.

    Raises:
        ValueError: If no devices match the provided filters.
    """

    return _run_nornir_task(
        napalm_get, name=name, group=group, platform=platform, getters=["facts"]
    )


@mcp.tool()
def nornir_run_getter(
    getter: str,
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    getter_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Runs any supported NAPALM getter on network device(s).

    Supports all standard NAPALM getters including arp_table, interfaces,
    routes, vlans, and more. Use nornir_list_getters to discover available
    getters for each platform.

    Args:
        getter: NAPALM getter name (e.g., 'arp_table', 'interfaces').
        name: Device name or list of names to query.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.
        getter_options: Optional getter-specific parameters passed to NAPALM.

    Returns:
        A dictionary mapping each device name to a HostResult containing
        the getter result in ``data`` on success, or ``error`` on failure.

    Raises:
        ValueError: If no devices match the provided filters.
    """

    g_opts = {getter: getter_options} if getter_options else None
    return _run_nornir_task(
        napalm_get,
        name=name,
        group=group,
        platform=platform,
        getters=[getter],
        getters_options=g_opts,
    )


@mcp.tool()
def nornir_get_config(
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    retrieve: str = "all",
    full: bool = False,
    sanitized: bool = False,
    format: str = "text",
) -> dict[str, Any]:
    """Retrieves device configuration from network device(s).

    Fetches running and/or startup configuration using NAPALM's config getter.

    Args:
        name: Device name or list of names to query.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.
        retrieve: Which config to retrieve — 'running', 'startup', or 'all'.
        full: If True, return the full configuration without filtering.
        sanitized: If True, remove sensitive data from the output.
        format: Configuration format — 'text' or 'json'.

    Returns:
        A dictionary mapping each device name to a HostResult. On success,
        ``data`` contains the configuration dict with 'running' and/or
        'startup' keys. On failure, ``error`` describes what went wrong.

    Raises:
        ValueError: If no devices match the provided filters.
    """

    getter_options = {
        "config": {
            "retrieve": retrieve,
            "full": full,
            "sanitized": sanitized,
            "format": format,
        }
    }
    return _run_nornir_task(
        napalm_get,
        name=name,
        group=group,
        platform=platform,
        getters=["config"],
        getters_options=getter_options,
    )


@mcp.tool()
def nornir_run_cli(
    commands: list[str],
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Executes CLI commands on network device(s) via NAPALM.

    Sends operational commands (e.g., 'show version') to devices and
    returns their output. Use only read-only commands for safety.

    Args:
        commands: List of CLI commands to execute on the devices.
        name: Device name or list of names to target.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.

    Returns:
        A dictionary mapping each device name to a HostResult. On success,
        ``data`` contains a dict of command-to-output mappings. On
        failure, ``error`` describes what went wrong.

    Raises:
        ValueError: If no devices match the provided filters.
    """

    return _run_nornir_task(
        napalm_cli, name=name, group=group, platform=platform, commands=commands
    )


@mcp.tool()
def nornir_list_getters() -> list[GetterInfo]:
    """Lists available NAPALM getters for each platform in the inventory.

    Introspects the NAPALM driver for each unique platform to discover
    which getters are supported. No device connection is required.

    Returns:
        A list of GetterInfo objects, one per platform, each containing
        the platform name and a sorted list of available getter names.
    """
    import napalm

    nr = get_nornir()
    platforms = {str(host.platform) for host in nr.inventory.hosts.values()}

    results = []
    for platform in sorted(platforms):
        try:
            driver = napalm.get_network_driver(platform)
            getters = sorted(
                name.removeprefix("get_")
                for name in dir(driver)
                if name.startswith("get_") and callable(getattr(driver, name))
            )
        except Exception as e:
            log.warning("Could not introspect driver for platform '%s': %s", platform, e)
            getters = []
        results.append(GetterInfo(platform=platform, getters=getters))

    return results


@mcp.tool()
def nornir_reload_inventory() -> None:
    """Reloads the network inventory from disk.

    Discards the in-memory inventory cache and re-reads YAML files.
    Use after editing the inventory files to pick up changes.
    """
    reset_nornir()
