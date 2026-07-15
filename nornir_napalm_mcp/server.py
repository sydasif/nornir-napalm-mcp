"""Nornir-NAPALM FastMCP Server — tool definitions only.

The FastMCP instance and CLI entry point live in ``main.py``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from nornir_napalm.plugins.tasks import napalm_cli, napalm_get

from nornir_napalm_mcp.introspection import list_getters
from nornir_napalm_mcp.models import GetterInfo, InventoryDevice
from nornir_napalm_mcp.runner import get_nornir, reset_nornir
from nornir_napalm_mcp.tasks import run_nornir_task

mcp = FastMCP(
    name="Nornir-NAPALM Server",
    instructions="Query network devices via NAPALM. Call nornir_list_inventory first.",
)


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
        ``data`` contains the facts dictionary. On failure, ``ok`` is False
        and ``error`` describes what went wrong.

    Raises:
        ValueError: If no devices match the provided filters.
    """
    return run_nornir_task(
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
    return run_nornir_task(
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
    return run_nornir_task(
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
    return run_nornir_task(
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
    return list_getters()


@mcp.tool()
def nornir_reload_inventory() -> None:
    """Reloads the network inventory from disk.

    Discards the in-memory inventory cache and re-reads YAML files.
    Use after editing the inventory files to pick up changes.
    """
    reset_nornir()
