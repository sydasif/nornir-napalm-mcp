"""Nornir-NAPALM FastMCP Server."""

import argparse
import logging
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from nornir_napalm_mcp.models import GetterInfo, InventoryDevice
from nornir_napalm_mcp.runner import _get_nornir, reset_nornir

if TYPE_CHECKING:
    from nornir.core import Nornir

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
    if name:
        if isinstance(name, str):
            name = [name]
        nr = nr.filter(filter_func=lambda h: h.name in name)
    if group:
        nr = nr.filter(filter_func=lambda h: group in [g.name for g in h.groups])
    if platform:
        nr = nr.filter(platform=platform)

    if not nr.inventory.hosts:
        available = ", ".join(sorted(_get_nornir().inventory.hosts)) or "(none)"
        raise ValueError(
            f"No devices match filters (name={name}, group={group}, platform={platform}). "
            f"Available devices: {available}. Call nornir_list_inventory."
        )

    return nr


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
) -> dict[str, Any]:
    """Retrieves system facts from network device(s) via NAPALM.

    Fetches device information such as hostname, vendor, model,
    OS version, and serial number.

    Args:
        name: Device name or list of names to query.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.

    Returns:
        A dictionary mapping each device name to its facts dictionary
        containing hostname, vendor, model, os_version, and serial_number.

    Raises:
        ValueError: If no devices match the provided filters.
    """
    from nornir_napalm.plugins.tasks import napalm_get

    nr = _filter_devices(_get_nornir(), name=name, group=group, platform=platform)
    result = nr.run(task=napalm_get, getters=["facts"])
    return {host: task[0].result for host, task in result.items()}


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
        A dictionary mapping each device name to the getter result.

    Raises:
        ValueError: If no devices match the provided filters.
    """
    from nornir_napalm.plugins.tasks import napalm_get

    nr = _filter_devices(_get_nornir(), name=name, group=group, platform=platform)

    g_opts = {getter: getter_options} if getter_options else None
    result = nr.run(task=napalm_get, getters=[getter], getters_options=g_opts)
    return {host: task[0].result for host, task in result.items()}


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
        A dictionary mapping each device name to its configuration data
        containing 'running' and/or 'startup' keys.

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
    return {host: task[0].result for host, task in result.items()}


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
        A dictionary mapping each device name to a dict of
        command-to-output mappings.

    Raises:
        ValueError: If no devices match the provided filters.
    """
    from nornir_napalm.plugins.tasks import napalm_cli

    nr = _filter_devices(_get_nornir(), name=name, group=group, platform=platform)
    result = nr.run(task=napalm_cli, commands=commands)
    return {host: task[0].result for host, task in result.items()}


@mcp.tool()
def nornir_ping(
    destination: str,
    name: str | list[str] | None = None,
    group: str | None = None,
    platform: str | None = None,
    source: str = "",
    ttl: int = 255,
    timeout: int = 2,
    size: int = 100,
    count: int = 5,
    vrf: str = "",
) -> dict[str, Any]:
    """Sends ICMP ping from network device(s) to a destination.

    Executes ping from the device itself (not from the MCP server) to test
    network reachability between the device and the target destination.

    Args:
        destination: IP address or hostname to ping.
        name: Device name or list of names to ping from.
        group: Group name to filter devices by.
        platform: Platform name to filter devices by.
        source: Source IP address or interface to ping from.
        ttl: Time-to-live value for ICMP packets.
        timeout: Timeout in seconds for each ping attempt.
        size: ICMP packet size in bytes.
        count: Number of ping packets to send.
        vrf: VRF name to ping within (for devices supporting VRFs).

    Returns:
        A dictionary mapping each device name to ping results including
        packets sent, received, loss percentage, and RTT statistics.

    Raises:
        ValueError: If no devices match the provided filters.
    """
    from nornir_napalm.plugins.tasks import napalm_ping

    nr = _filter_devices(_get_nornir(), name=name, group=group, platform=platform)

    result = nr.run(
        task=napalm_ping,
        dest=destination,
        source=source,
        ttl=ttl,
        timeout=timeout,
        size=size,
        count=count,
        vrf=vrf,
    )
    return {host: task[0].result for host, task in result.items()}


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
        except Exception:
            log.warning("Could not introspect driver for platform '%s'", platform)
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
