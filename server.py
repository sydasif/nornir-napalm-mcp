"""
Nornir-NAPALM FastMCP Server
Exposes network device data to AI assistants via NAPALM getters.
"""

import argparse
import logging
import os
from operator import attrgetter
from pathlib import Path
from typing import Any, cast

import napalm
from fastmcp import FastMCP
from nornir import InitNornir
from nornir.core import Nornir
from nornir_napalm.plugins.tasks import napalm_cli, napalm_get
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("nornir-napalm-mcp")

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class InventoryDevice(BaseModel):
    """Structured representation of a network device in the inventory."""

    name: str
    hostname: str
    platform: str
    groups: list[str]


class NetworkFacts(BaseModel):
    """System facts for a network device."""

    hostname: str | None = None
    vendor: str | None = None
    model: str | None = None
    os_version: str | None = None
    serial_number: str | None = None
    additional_facts: dict[str, Any] = Field(default_factory=dict)


class NetworkInterfaces(BaseModel):
    """Interface and IP address data for a network device."""

    interfaces: dict[str, Any] = Field(default_factory=dict)
    interfaces_ip: dict[str, Any] = Field(default_factory=dict)


class DeviceConfig(BaseModel):
    """Running and/or startup configuration for a network device."""

    running: str | None = None
    startup: str | None = None


class ReloadSummary(BaseModel):
    """Summary of inventory reload changes."""

    previous_hosts: list[str]
    current_hosts: list[str]
    added: list[str]
    removed: list[str]
    total: int


class GetterInfo(BaseModel):
    """Available NAPALM getters for a given platform."""

    platform: str
    getters: list[str]


# ---------------------------------------------------------------------------
# FastMCP + Nornir initialisation
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="Nornir-NAPALM Server",
    instructions=(
        "Query live network device state via NAPALM getters. "
        "Call nornir_list_inventory first to discover available devices, "
        "then use the targeted getter tools. All operations are read-only."
    ),
)

_nornir: Nornir | None = None


def _resolve_config() -> Path:
    """Resolve the Nornir config file path and switch to its directory.

    Honors the NORNIR_CONFIG env var. Relative paths are resolved against
    the directory containing this server module.

    Changes the working directory to the config file's parent so that
    Nornir's SimpleInventory can resolve relative inventory file paths
    (host_file, group_file, defaults_file) correctly regardless of how
    the server was launched.

    Returns:
        The absolute path to the Nornir configuration file.
    """
    raw = os.environ.get("NORNIR_CONFIG", "config.yaml")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    config_path = path.resolve()
    os.chdir(config_path.parent)
    return config_path


def _get_nornir() -> Nornir:
    """Return the cached Nornir instance, initialising on first call.

    Lazy initialisation means a broken inventory does not prevent the
    MCP server from starting and exposing its tool catalogue.

    Returns:
        The initialized Nornir instance.
    """
    global _nornir
    if _nornir is None:
        config_path = _resolve_config()
        log.info("Initialising Nornir from %s", config_path)
        _nornir = InitNornir(config_file=str(config_path))
        log.info("Nornir initialised with %d hosts.", len(_nornir.inventory.hosts))
    return _nornir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_device(nr: Nornir, device_name: str) -> None:
    """Validate that a device exists in the Nornir inventory.

    Args:
        nr: The Nornir instance.
        device_name: Host name to look up.

    Raises:
        ValueError: If the device is not found in the inventory.
    """
    if device_name not in nr.inventory.hosts:
        available = ", ".join(sorted(nr.inventory.hosts)) or "(none)"
        raise ValueError(
            f"Device '{device_name}' not found in inventory. "
            f"Available devices: {available}. Call nornir_list_inventory to see the current list."
        )


def _extract_single_result(result: dict[str, Any], device_name: str) -> dict[str, Any]:
    """Extract the task result from Nornir's MultiResult for a single host.

    Args:
        result: The AggregatedResult from nr.run().
        device_name: The host name key to extract.

    Returns:
        The raw result dict from the task.

    Raises:
        RuntimeError: If the task failed or returned no result.
    """
    host_result = result[device_name]
    task_result = host_result[0]

    if task_result.failed:
        raise RuntimeError(f"NAPALM task failed for '{device_name}': {task_result.exception}")

    return cast(dict[str, Any], task_result.result)


def _run_getter(device_name: str, getters: list[str]) -> dict[str, Any]:
    """Filter Nornir to a single host and run napalm_get.

    Args:
        device_name: Exact host name as defined in hosts.yaml.
        getters: List of NAPALM getters to execute.

    Returns:
        The raw getter dict from the task result.

    Raises:
        ValueError: For unknown devices.
        RuntimeError: For connection or task failures.
    """
    nr = _get_nornir()
    _resolve_device(nr, device_name)

    nr_filtered = nr.filter(name=device_name)
    result = nr_filtered.run(task=napalm_get, getters=getters)
    return _extract_single_result(result, device_name)


def _run_cli(device_name: str, commands: list[str]) -> dict[str, str]:
    """Filter Nornir to a single host and run napalm_cli.

    Args:
        device_name: Exact host name as defined in hosts.yaml.
        commands: List of CLI commands to execute (must be read-only show commands).

    Returns:
        A dict mapping each command to its output text.

    Raises:
        ValueError: For unknown devices.
        RuntimeError: For connection or task failures.
    """
    nr = _get_nornir()
    _resolve_device(nr, device_name)

    nr_filtered = nr.filter(name=device_name)
    result = nr_filtered.run(task=napalm_cli, commands=commands)
    return cast(dict[str, str], _extract_single_result(result, device_name))


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
    """
    if not getter.replace("_", "").isalpha():
        raise ValueError(
            f"Invalid getter name '{getter}'. "
            "Use lowercase letters and underscores only (e.g. 'arp_table')."
        )

    data = _run_getter(device_name, [getter])
    return data.get(getter, data)


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
    config_data = data.get("config", data)

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


@mcp.tool()
def nornir_list_getters() -> list[GetterInfo]:
    """List available NAPALM getters for each platform in the inventory.

    Introspects the NAPALM driver for each unique platform to discover which
    getters are supported. No device connection is required — this is instant.

    Returns:
        A list of GetterInfo objects, one per platform found in the inventory.
    """
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
        except (ModuleNotFoundError, TypeError):
            log.warning("Could not introspect NAPALM driver for platform '%s'", platform)
            getters = []
        results.append(GetterInfo(platform=platform, getters=getters))

    return results


@mcp.tool()
def nornir_reload_inventory() -> ReloadSummary:
    """Reload the network inventory from disk.

    Discards the in-memory inventory cache and re-reads YAML files.
    Use after editing the inventory files.

    Returns:
        A structured ReloadSummary containing added, removed, and total hosts.
    """
    global _nornir
    previous = sorted(_nornir.inventory.hosts) if _nornir is not None else []
    _nornir = None
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
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport (default: stdio for Claude Desktop)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="SSE bind host")
    parser.add_argument("--port", type=int, default=8000, help="SSE bind port")
    args = parser.parse_args()

    match args.transport:
        case "sse":
            log.info("Starting SSE server on %s:%d", args.host, args.port)
            mcp.run(transport="sse", host=args.host, port=args.port)
        case "stdio":
            log.info("Starting STDIO server (Claude Desktop mode)")
            mcp.run(transport="stdio")
        case _:  # pragma: no cover
            pass


if __name__ == "__main__":
    main()
