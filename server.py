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
# FastMCP + Nornir initialisation
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="Nornir-NAPALM Server",
    instructions=(
        "Query live network device state via NAPALM getters. "
        "Call list_inventory first to discover available devices, "
        "then use the targeted getter tools. All operations are read-only."
    ),
)

_nornir: Any | None = None


def _resolve_config() -> str:
    """Resolve the Nornir config file path.

    Honors the NORNIR_CONFIG env var. Relative paths are resolved against
    the directory containing this server module, so the server runs
    correctly regardless of the current working directory.
    """
    raw = os.environ.get("NORNIR_CONFIG", "config.yaml")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return str(path)


def _get_nornir() -> Any:
    """Return the cached Nornir instance, initialising on first call.

    Lazy initialisation means a broken inventory does not prevent the
    MCP server from starting and exposing its tool catalogue. Errors
    surface on the first tool call, where they can be handled cleanly.
    """
    global _nornir
    if _nornir is None:
        config_path = _resolve_config()
        log.info("Initialising Nornir from %s", config_path)
        _nornir = InitNornir(config_file=config_path)
        log.info("Nornir initialised with %d hosts.", len(_nornir.inventory.hosts))
    return _nornir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_host(device_name: str) -> Any:
    """Return the Host object or raise a descriptive ValueError."""
    nr = _get_nornir()
    host = nr.inventory.hosts.get(device_name)
    if host is None:
        available = ", ".join(sorted(nr.inventory.hosts)) or "(none)"
        raise ValueError(
            f"Device '{device_name}' not found in inventory. Available devices: {available}"
        )
    return host


def _run_getter(device_name: str, getters: list[str]) -> dict[str, Any]:
    """
    Filter Nornir to a single host and run napalm_get.

    Returns the raw getter dict from the task result.
    Raises ValueError for unknown devices, RuntimeError for connection/task failures.
    """
    _get_host(device_name)  # validate early with a friendly message

    nr = _get_nornir()
    nr_filtered = nr.filter(name=device_name)
    result = nr_filtered.run(task=napalm_get, getters=getters)

    host_result = result[device_name]  # MultiResult (list of TaskResult)
    task_result = host_result[0]  # first (and only) TaskResult

    if task_result.failed:
        raise RuntimeError(f"NAPALM task failed for '{device_name}': {task_result.exception}")

    return task_result.result  # dict keyed by getter name


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_inventory() -> list[dict[str, Any]]:
    """
    List all network devices loaded from the YAML inventory.

    Returns hostname, platform, and group membership for each device.
    Always call this first to discover what devices are available before
    running any getter tool.
    """
    nr = _get_nornir()
    devices = []
    for host in nr.inventory.hosts.values():
        devices.append(
            {
                "name": host.name,
                "hostname": str(host.hostname),
                "platform": str(host.platform),
                "groups": [g.name for g in host.groups],
            }
        )
    return sorted(devices, key=lambda d: d["name"])


@mcp.tool()
def get_network_facts(device_name: str) -> dict[str, Any]:
    """
    Fetch system facts for a specific device.

    Returns: hostname, vendor, model, os_version, serial_number, uptime,
    and interface list.

    Args:
        device_name: Exact host name as defined in hosts.yaml (case-sensitive).
    """
    data = _run_getter(device_name, ["facts"])
    facts = data.get("facts")
    if facts is None:
        raise RuntimeError(f"NAPALM 'facts' getter returned no data for '{device_name}'.")
    return facts


@mcp.tool()
def get_network_interfaces(device_name: str) -> dict[str, Any]:
    """
    Fetch interface details and IP address assignments for a specific device.

    Returns a merged dict with two keys:
      - "interfaces":    physical state, speed, MTU, MAC per interface.
      - "interfaces_ip": IP/prefix assignments per interface.

    Args:
        device_name: Exact host name as defined in hosts.yaml (case-sensitive).
    """
    data = _run_getter(device_name, ["interfaces", "interfaces_ip"])
    return {
        "interfaces": data.get("interfaces", {}),
        "interfaces_ip": data.get("interfaces_ip", {}),
    }


@mcp.tool()
def run_napalm_getter(device_name: str, getter: str) -> Any:
    """
    Run any supported NAPALM getter on a specific device.

    Useful for getters not covered by dedicated tools. Common getters:
      arp_table, bgp_neighbors, bgp_neighbors_detail, bgp_config,
      environment, lldp_neighbors, lldp_neighbors_detail,
      mac_address_table, ntp_servers, ntp_stats,
      optics, route_to, snmp_information, users, vlans.

    Args:
        device_name: Exact host name as defined in hosts.yaml (case-sensitive).
        getter:      NAPALM getter name (without the "get_" prefix).
    """
    if not getter.replace("_", "").isalpha():
        raise ValueError(
            f"Invalid getter name '{getter}'. "
            "Use lowercase letters and underscores only (e.g. 'arp_table')."
        )

    data = _run_getter(device_name, [getter])
    return data.get(getter, data)


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
