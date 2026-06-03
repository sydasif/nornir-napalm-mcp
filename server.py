"""
Nornir-NAPALM FastMCP Server
Exposes network device data to AI assistants via NAPALM getters.
"""

import logging
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from nornir import InitNornir
from nornir.core import Nornir
from nornir.core.inventory import Host
from nornir_napalm.plugins.tasks import napalm_get

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


class ReloadSummary(BaseModel):
    """Summary of inventory reload changes."""

    previous_hosts: list[str]
    current_hosts: list[str]
    added: list[str]
    removed: list[str]
    total: int


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
    """Resolve the Nornir config file path.

    Honors the NORNIR_CONFIG env var. Relative paths are resolved against
    the directory containing this server module.

    Returns:
        The absolute path to the Nornir configuration file.
    """
    raw = os.environ.get("NORNIR_CONFIG", "config.yaml")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path.resolve()


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


def _get_host(device_name: str) -> Host:
    """Return the Host object or raise a descriptive ValueError.

    Args:
        device_name: The name of the host to retrieve.

    Returns:
        The Nornir Host object.

    Raises:
        ValueError: If the device is not found in the inventory.
    """
    nr = _get_nornir()
    host = nr.inventory.hosts.get(device_name)
    if host is None:
        available = ", ".join(sorted(nr.inventory.hosts)) or "(none)"
        raise ValueError(
            f"Device '{device_name}' not found in inventory. "
            f"Available devices: {available}. Call nornir_list_inventory to see the current list."
        )
    return host


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
    _get_host(device_name)  # validate early with a friendly message

    nr = _get_nornir()
    nr_filtered = nr.filter(name=device_name)
    result = nr_filtered.run(task=napalm_get, getters=getters)

    # Nornir MultiResult mapping
    host_result = result[device_name]
    task_result = host_result[0]

    if task_result.failed:
        raise RuntimeError(f"NAPALM task failed for '{device_name}': {task_result.exception}")

    return task_result.result  # dict keyed by getter name


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
    return sorted(devices, key=lambda d: d.name)


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

    standard_fields = {"hostname", "vendor", "model", "os_version", "serial_number"}
    fact_dict = facts_data if isinstance(facts_data, dict) else {}

    return NetworkFacts(
        **{k: v for k, v in fact_dict.items() if k in standard_fields},
        additional_facts={k: v for k, v in fact_dict.items() if k not in standard_fields},
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

if __name__ == "__main__":
    import argparse

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

    if args.transport == "sse":
        log.info("Starting SSE server on %s:%d", args.host, args.port)
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        log.info("Starting STDIO server (Claude Desktop mode)")
        mcp.run(transport="stdio")
