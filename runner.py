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
        RuntimeError: If the task failed, returned no result, or device is missing.
    """
    if device_name not in result:
        raise RuntimeError(
            f"No result returned for '{device_name}'. "
            "The device may not exist or the filter matched no hosts."
        )
    host_result = result[device_name]
    if not host_result:
        raise RuntimeError(f"Empty result for '{device_name}'. The task produced no output.")
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
