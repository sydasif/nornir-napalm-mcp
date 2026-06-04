"""
Nornir-NAPALM FastMCP Server
Exposes network device data to AI assistants via NAPALM getters.
"""

import argparse
import logging
from operator import attrgetter
from typing import Any

import napalm
from fastmcp import FastMCP

from models import (
    DeviceConfig,
    GetterInfo,
    InventoryDevice,
    NetworkFacts,
    NetworkInterfaces,
    ReloadSummary,
)
from runner import (
    _get_nornir,
    _run_cli,
    _run_getter,
    reset_nornir,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("nornir-napalm-mcp")

# ---------------------------------------------------------------------------
# FastMCP initialisation
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="Nornir-NAPALM Server",
    instructions=(
        "Query live network device state via NAPALM getters. "
        "Call nornir_list_inventory first to discover available devices, "
        "then use the targeted getter tools. All operations are read-only."
    ),
)

# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def nornir_list_inventory() -> list[InventoryDevice]:
    """List all network devices loaded from the YAML inventory.

    Returns a list of devices with their hostname, platform, and group membership.
    Always call this first to discover what devices are available.

    Returns:
        A sorted list of devices.
    """
    nr = _get_nornir()
    devices: list[InventoryDevice] = []
    for host in nr.inventory.hosts.values():
        devices.append(
            InventoryDevice(
                name=host.name,
                hostname=str(host.hostname),
                platform=str(host.platform),
                groups=[g.name for g in host.groups],
            )
        )
    return sorted(devices, key=attrgetter("name"))


@mcp.tool()
def nornir_get_facts(device_name: str) -> NetworkFacts:
    """Fetch system facts for a specific device.

    Args:
        device_name: Exact host name as defined in hosts.yaml (case-sensitive).

    Returns:
        A structured NetworkFacts object containing hostname, vendor, model, etc.

    Raises:
        RuntimeError: If the 'facts' getter returns no data.
    """
    data = _run_getter(device_name, ["facts"])
    facts_data = data.get("facts")
    if facts_data is None:
        raise RuntimeError(
            f"NAPALM 'facts' getter returned no data for '{device_name}'. "
            "Check the device connectivity or try nornir_run_getter with a different getter."
        )

    if not isinstance(facts_data, dict):
        raise RuntimeError(
            f"NAPALM 'facts' getter returned unexpected type {type(facts_data).__name__} "
            f"for '{device_name}'. Expected a dict."
        )

    standard_fields = {"hostname", "vendor", "model", "os_version", "serial_number"}

    return NetworkFacts(
        **{k: v for k, v in facts_data.items() if k in standard_fields},
        additional_facts={k: v for k, v in facts_data.items() if k not in standard_fields},
    )


@mcp.tool()
def nornir_get_interfaces(device_name: str) -> NetworkInterfaces:
    """Fetch interface details and IP address assignments for a specific device.

    Args:
        device_name: Exact host name as defined in hosts.yaml (case-sensitive).

    Returns:
        A structured NetworkInterfaces object.
    """
    data = _run_getter(device_name, ["interfaces", "interfaces_ip"])
    return NetworkInterfaces(
        interfaces=data.get("interfaces", {}),
        interfaces_ip=data.get("interfaces_ip", {}),
    )


@mcp.tool()
def nornir_run_getter(device_name: str, getter: str) -> Any:
    """Run any supported NAPALM getter on a specific device.

    Useful for getters not covered by dedicated tools (e.g., 'arp_table', 'vlans').

    Args:
        device_name: Exact host name as defined in hosts.yaml (case-sensitive).
        getter: NAPALM getter name (without the "get_" prefix).

    Returns:
        The result of the NAPALM getter.

    Raises:
        ValueError: If the getter name contains invalid characters.
        RuntimeError: If the getter key is missing from the response.
    """
    if not getter.replace("_", "").isalnum():
        raise ValueError(
            f"Invalid getter name '{getter}'. "
            "Use lowercase letters, digits, and underscores only (e.g. 'arp_table')."
        )

    data = _run_getter(device_name, [getter])
    if getter not in data:
        raise RuntimeError(
            f"NAPALM getter '{getter}' returned unexpected response structure "
            f"for '{device_name}'. Expected key '{getter}' not found in result."
        )
    return data[getter]


@mcp.tool()
def nornir_get_config(
    device_name: str,
    config_type: str = "both",
) -> DeviceConfig:
    """Retrieve the running and/or startup configuration from a device.

    Uses NAPALM's get_config getter to fetch configuration files. Note that
    configuration output may contain sensitive information such as passwords
    or community strings.

    Args:
        device_name: Exact host name as defined in hosts.yaml (case-sensitive).
        config_type: Which config to retrieve — 'running', 'startup', or 'both' (default).

    Returns:
        A DeviceConfig object with running and/or startup configuration text.

    Raises:
        ValueError: If config_type is not one of 'running', 'startup', or 'both'.
        RuntimeError: If the config getter returns no data.
    """
    valid_types = {"running", "startup", "both"}
    if config_type not in valid_types:
        raise ValueError(
            f"Invalid config_type '{config_type}'. "
            f"Must be one of: {', '.join(sorted(valid_types))}."
        )

    data = _run_getter(device_name, ["config"])
    config_data = data.get("config")
    if config_data is None:
        raise RuntimeError(
            f"NAPALM 'config' getter returned no data for '{device_name}'. "
            "Check device connectivity."
        )

    if not isinstance(config_data, dict):
        raise RuntimeError(
            f"NAPALM 'config' getter returned unexpected type {type(config_data).__name__} "
            f"for '{device_name}'. Expected a dict."
        )

    running = config_data.get("running") if config_type in ("running", "both") else None
    startup = config_data.get("startup") if config_type in ("startup", "both") else None

    return DeviceConfig(running=running, startup=startup)


@mcp.tool()
def nornir_run_cli(device_name: str, commands: list[str]) -> dict[str, str]:
    """Execute read-only CLI commands on a device and return their output.

    Sends operational commands (e.g., 'show ip interface brief') via NAPALM's
    cli() method. Only 'show' commands are permitted for safety — configuration
    commands are rejected.

    Args:
        device_name: Exact host name as defined in hosts.yaml (case-sensitive).
        commands: List of CLI commands to execute (must start with 'show').

    Returns:
        A dict mapping each command string to its output text.

    Raises:
        ValueError: If any command does not start with 'show', or device not found.
        RuntimeError: For connection or task failures.
    """
    if not commands:
        raise ValueError("No commands provided. Pass at least one 'show' command.")

    for cmd in commands:
        if not cmd.strip().lower().startswith("show"):
            raise ValueError(
                f"Command '{cmd}' is not a read-only show command. "
                "Only 'show' commands are permitted for safety."
            )

    return _run_cli(device_name, commands)


_getters_cache: list[GetterInfo] | None = None


@mcp.tool()
def nornir_list_getters() -> list[GetterInfo]:
    """List available NAPALM getters for each platform in the inventory.

    Introspects the NAPALM driver for each unique platform to discover which
    getters are supported. No device connection is required — this is instant.
    Results are cached until nornir_reload_inventory is called.

    Returns:
        A list of GetterInfo objects, one per platform found in the inventory.
    """
    global _getters_cache
    if _getters_cache is not None:
        return _getters_cache

    nr = _get_nornir()
    platforms = {str(host.platform) for host in nr.inventory.hosts.values()}

    results: list[GetterInfo] = []
    for platform in sorted(platforms):
        try:
            driver = napalm.get_network_driver(platform)
            # Introspect the driver class for get_* methods
            getters = sorted(
                name.removeprefix("get_")
                for name in dir(driver)
                if name.startswith("get_") and callable(getattr(driver, name))
            )
        except Exception as exc:
            if (
                not isinstance(exc, (ModuleNotFoundError, TypeError))
                and not type(exc).__name__ == "ModuleImportError"
            ):
                raise
            log.warning("Could not introspect NAPALM driver for platform '%s'", platform)
            getters = []
        results.append(GetterInfo(platform=platform, getters=getters))

    _getters_cache = results
    return results


@mcp.tool()
def nornir_reload_inventory() -> ReloadSummary:
    """Reload the network inventory from disk.

    Discards the in-memory inventory cache and re-reads YAML files.
    Use after editing the inventory files.

    Returns:
        A structured ReloadSummary containing added, removed, and total hosts.
    """
    global _getters_cache
    previous_nornir = reset_nornir()
    previous = sorted(previous_nornir.inventory.hosts) if previous_nornir is not None else []
    _getters_cache = None

    rebuilt = _get_nornir()
    current = sorted(rebuilt.inventory.hosts)
    return ReloadSummary(
        previous_hosts=previous,
        current_hosts=current,
        added=sorted(set(current) - set(previous)),
        removed=sorted(set(previous) - set(current)),
        total=len(current),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the nornir-napalm-mcp CLI."""

    parser = argparse.ArgumentParser(description="Nornir-NAPALM FastMCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport (default: stdio for Claude Desktop)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP bind port")
    args = parser.parse_args()

    match args.transport:
        case "http":
            log.info("Starting HTTP server on %s:%d", args.host, args.port)
            mcp.run(transport="http", host=args.host, port=args.port)
        case "stdio":
            log.info("Starting STDIO server (Claude Desktop mode)")
            mcp.run(transport="stdio")
        case _:  # pragma: no cover
            pass


if __name__ == "__main__":
    main()
