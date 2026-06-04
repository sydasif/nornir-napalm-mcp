"""Nornir initialization and helper functions for the MCP Server."""

import logging
import os
import threading
from pathlib import Path
from typing import Any, cast

from nornir import InitNornir
from nornir.core import Nornir
from nornir_napalm.plugins.tasks import napalm_cli, napalm_get

log = logging.getLogger("nornir-napalm-mcp")

_nornir: Nornir | None = None
_nornir_lock = threading.Lock()


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
    Thread-safe via double-checked locking.

    Returns:
        The initialized Nornir instance.
    """
    global _nornir
    if _nornir is None:
        with _nornir_lock:
            if _nornir is None:
                config_path = _resolve_config()
                log.info("Initialising Nornir from %s", config_path)
                _nornir = InitNornir(config_file=str(config_path))
                log.info("Nornir initialised with %d hosts.", len(_nornir.inventory.hosts))
    return _nornir


def reset_nornir() -> Nornir | None:
    """Reset the cached Nornir instance.

    Returns the previous instance (or None if not initialised).
    Thread-safe via the same lock as _get_nornir().
    """
    global _nornir
    with _nornir_lock:
        previous = _nornir
        _nornir = None
    return previous


def _resolve_filter(nr: Nornir, device_names: str | list[str]) -> Nornir:
    """Filter Nornir by a list of device names and validate that all devices exist.

    Args:
        nr: The Nornir instance.
        device_names: A single device name or a list of device names.

    Returns:
        A filtered Nornir instance containing only the specified devices.

    Raises:
        ValueError: If any device is not found in the inventory.
    """
    if isinstance(device_names, str):
        device_names = [device_names]

    # Check that all devices exist
    nr_filtered = nr.filter(name__in=device_names)
    available = ", ".join(sorted(nr.inventory.hosts)) or "(none)"

    if len(nr_filtered.inventory.hosts) == 0:
        raise ValueError(
            f"No devices found matching {device_names}. "
            f"Available devices: {available}. Call nornir_list_inventory to see the current list."
        )
    # Ensure we found all requested devices
    found_names = set(nr_filtered.inventory.hosts.keys())
    requested_names = set(device_names)
    if not requested_names.issubset(found_names):
        missing = requested_names - found_names
        raise ValueError(
            f"Following devices not found in inventory: {', '.join(sorted(missing))}. "
            f"Available devices: {available}."
        )
    return nr_filtered


def _extract_multiple_result(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract task results from Nornir's MultiResult for multiple hosts.

    Args:
        result: The AggregatedResult from nr.run().

    Returns:
        A dict mapping host name to the result dict.

    Raises:
        RuntimeError: If the task failed, returned no result, or host is missing.
    """
    extracted: dict[str, dict[str, Any]] = {}
    for host_name, multi_result in result.items():
        if not multi_result:
            raise RuntimeError(f"Empty result for '{host_name}'. The task produced no output.")
        task_result = multi_result[0]
        if task_result.failed:
            raise RuntimeError(f"NAPALM task failed for '{host_name}': {task_result.exception}")
        extracted[host_name] = cast(dict[str, Any], task_result.result)
    return extracted


def _run_getter(device_name: str | list[str], getters: list[str]) -> dict[str, dict[str, Any]]:
    """Filter Nornir by device name(s) and run napalm_get.

    Args:
        device_name: Exact host name as defined in hosts.yaml, or a list of host names.
        getters: List of NAPALM getters to execute.

    Returns:
        A dict mapping each host name to its raw getter dict.

    Raises:
        ValueError: For unknown devices.
        RuntimeError: For connection or task failures.
    """
    nr = _get_nornir()
    nr_filtered = _resolve_filter(nr, device_name)
    result = nr_filtered.run(task=napalm_get, getters=getters)
    return _extract_multiple_result(result)


def _run_cli(device_name: str | list[str], commands: list[str]) -> dict[str, dict[str, str]]:
    """Filter Nornir by device name(s) and run napalm_cli.

    Args:
        device_name: Exact host name as defined in hosts.yaml, or a list of host names.
        commands: List of CLI commands to execute (must be read-only show commands).

    Returns:
        A dict mapping each host name to a dict of command outputs.

    Raises:
        ValueError: For unknown devices.
        RuntimeError: For connection or task failures.
    """
    nr = _get_nornir()
    nr_filtered = _resolve_filter(nr, device_name)
    result = nr_filtered.run(task=napalm_cli, commands=commands)
    # We know the result is dict[str, str] for each host, so we cast accordingly
    extracted: dict[str, dict[str, str]] = {}
    for host_name, multi_result in result.items():
        if not multi_result:
            raise RuntimeError(f"Empty result for '{host_name}'. The task produced no output.")
        task_result = multi_result[0]
        if task_result.failed:
            raise RuntimeError(f"NAPALM task failed for '{host_name}': {task_result.exception}")
        extracted[host_name] = cast(dict[str, str], task_result.result)
    return extracted
