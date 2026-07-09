"""Nornir-NAPALM FastMCP Server."""

import argparse
import logging
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from nornir_napalm_mcp.models import GetterInfo, HostResult, InventoryDevice
from nornir_napalm_mcp.runner import _get_nornir, reset_nornir

if TYPE_CHECKING:
    from nornir.core import Nornir
    from nornir.core.task import AggregatedResult

logging.basicConfig(level=logging.INFO)
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
        if isinstance(name, str):
            name = [name]
        nr = nr.filter(filter_func=lambda h: h.name in name)
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


def _raw_result(result: "AggregatedResult") -> dict[str, Any]:
    """Extract the original per-host dict that the tools returned before refactor.

    The original implementation did ``{host: task[0].result for host, task in result.items()}``.
    This helper reproduces that shape so callers can request the legacy format via ``raw=True``.
    """
    raw: dict[str, Any] = {}
    for host, multi_result in result.items():
        if not multi_result:
            raw[host] = None
        else:
            raw[host] = multi_result[0].result
    return raw


@mcp.tool()
def nornir_list_inventory() -> list[InventoryDevice]:
    """Lists all devices in the Nornir inventory.

    Returns:
        A sorted list of InventoryDevice objects, each containing
        the device name, hostname, platform, and group membership.
    """
    nr = _get_nornir()
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
    raw: bool = False,
) -> dict[str, Any]:
    """Retrieves system facts from network device(s) via NAPALM.

    Fetches device information such as hostname, vendor, model,
    OS version, and serial number.

    Args:
        name: Device name or list of names to query.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.
        raw: If True, return the legacy raw dict (host → result); if False (default) return HostResult objects.

    Returns:
        A dictionary mapping each device name to a HostResult. On success,
        ``data`` contains the facts dictionary (hostname, vendor, model,
        os_version, serial_number). On failure, ``ok`` is False and
        ``error`` describes what went wrong.

    Raises:
        ValueError: If no devices match the provided filters.
    """
    from nornir_napalm.plugins.tasks import napalm_get

    nr = _filter_devices(_get_nornir(), name=name, group=group, platform=platform)
    result = nr.run(task=napalm_get, getters=["facts"])
    return _raw_result(result) if raw else _result_to_dict(result)


@mcp.tool()
def nornir_run_getter(
    getter: str,
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    getter_options: dict[str, Any] | None = None,
    raw: bool = False,
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
        raw: If True, return the legacy raw dict (host → result); if False (default) return HostResult objects.
        getter_options: Optional getter-specific parameters passed to NAPALM.

    Returns:
        A dictionary mapping each device name to a HostResult containing
        the getter result in ``data`` on success, or ``error`` on failure.

    Raises:
        ValueError: If no devices match the provided filters.
    """
    from nornir_napalm.plugins.tasks import napalm_get

    nr = _filter_devices(_get_nornir(), name=name, group=group, platform=platform)

    g_opts = {getter: getter_options} if getter_options else None
    result = nr.run(task=napalm_get, getters=[getter], getters_options=g_opts)
    return _raw_result(result) if raw else _result_to_dict(result)


@mcp.tool()
def nornir_get_config(
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    retrieve: str = "all",
    full: bool = False,
    sanitized: bool = False,
    format: str = "text",
    raw: bool = False,
) -> dict[str, Any]:
    """Retrieves device configuration from network device(s).

    Fetches running and/or startup configuration using NAPALM's config getter.

    Args:
        name: Device name or list of names to query.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.
        raw: If True, return the legacy raw dict (host → result); if False (default) return HostResult objects.
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
    from nornir_napalm.plugins.tasks import napalm_get

    nr = _filter_devices(_get_nornir(), name=name, group=group, platform=platform)

    getter_options = {
        "config": {
            "retrieve": retrieve,
            "full": full,
            "sanitized": sanitized,
            "format": format,
        }
    }

    result = nr.run(task=napalm_get, getters=["config"], getters_options=getter_options)
    return _raw_result(result) if raw else _result_to_dict(result)


@mcp.tool()
def nornir_run_cli(
    commands: list[str],
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """Executes CLI commands on network device(s) via NAPALM.

    Sends operational commands (e.g., 'show version') to devices and
    returns their output. Use only read-only commands for safety.

    Args:
        commands: List of CLI commands to execute on the devices.
        name: Device name or list of names to target.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.
        raw: If True, return the legacy raw dict (host → result); if False (default) return HostResult objects.

    Returns:
        A dictionary mapping each device name to a HostResult. On success,
        ``data`` contains a dict of command-to-output mappings. On
        failure, ``error`` describes what went wrong.

    Raises:
        ValueError: If no devices match the provided filters.
    """
    from nornir_napalm.plugins.tasks import napalm_cli

    nr = _filter_devices(_get_nornir(), name=name, group=group, platform=platform)
    result = nr.run(task=napalm_cli, commands=commands)
    return _raw_result(result) if raw else _result_to_dict(result)


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

    nr = _get_nornir()
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
