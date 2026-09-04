"""Config-capture service: shared by NornirBase.backup and NetmikoTool apply path.

:nfunc:`capture_running_config` reads one device's running configuration and
returns it as text. NAPALM is preferred; platforms without a NAPALM driver
fall back to netmiko's ``show running-config``. Failures raise
:exc:`McpError` subclasses and propagate to the caller.
"""

from __future__ import annotations

from typing import Any

import napalm
from nornir_napalm.plugins.tasks import napalm_get
from nornir_netmiko.tasks import netmiko_send_command

from nornir_mcp.core.errors import DeviceConnectionError, InternalError
from nornir_mcp.core.tasks import run_nornir_task

_CAPTURE_OPERATION = "config_capture"


def capture_running_config(host: str, platform: str, ctx_request_id: str) -> str:
    """Return the running configuration for *host* as text.

    Args:
        host: Device name.
        platform: NAPALM platform name (used to pick the capture engine).
        ctx_request_id: Request correlation id, included in error messages.

    Returns:
        The running configuration text.

    Raises:
        DeviceConnectionError: If the device capture failed at connection
            level (retryable).
        InternalError: For any other capture failure (non-retryable).
    """
    try:
        napalm.get_network_driver(platform)
    except Exception:
        # No NAPALM driver for this platform: fall back to the CLI.
        return _capture_via_netmiko(host, ctx_request_id)
    return _capture_via_napalm(host, ctx_request_id)


def _capture_via_napalm(host: str, ctx_request_id: str) -> str:
    outcomes = run_nornir_task(
        napalm_get,
        operation=_CAPTURE_OPERATION,
        name=host,
        getters=["config"],
        getters_options={"config": {"retrieve": "running", "full": True}},
    )
    outcome = outcomes.get(host)
    if outcome is None:
        raise InternalError(
            f"config capture for '{host}' returned no result (request {ctx_request_id})",
            host=host,
            operation=_CAPTURE_OPERATION,
        )
    if not outcome.success:
        _raise_capture_error(outcome.error, host, ctx_request_id)
    data = outcome.data if isinstance(outcome.data, dict) else {}
    config = data.get("config") if isinstance(data, dict) else None
    running = config.get("running") if isinstance(config, dict) else None
    if not isinstance(running, str):
        raise InternalError(
            f"no running config text in capture for '{host}' (request {ctx_request_id})",
            host=host,
            operation=_CAPTURE_OPERATION,
        )
    return running


def _capture_via_netmiko(host: str, ctx_request_id: str) -> str:
    outcomes = run_nornir_task(
        netmiko_send_command,
        operation=_CAPTURE_OPERATION,
        name=host,
        command_string="show running-config",
    )
    outcome = outcomes.get(host)
    if outcome is None:
        raise InternalError(
            f"config capture for '{host}' returned no result (request {ctx_request_id})",
            host=host,
            operation=_CAPTURE_OPERATION,
        )
    if not outcome.success:
        _raise_capture_error(outcome.error, host, ctx_request_id)
    return outcome.data if isinstance(outcome.data, str) else str(outcome.data or "")


def _raise_capture_error(error: Any, host: str, ctx_request_id: str) -> None:
    """Convert a structured host failure into a categorized McpError."""
    message = (
        error.message
        if error is not None and hasattr(error, "message")
        else "config capture failed"
    )
    if error is not None and getattr(error, "type", None) == "connection":
        raise DeviceConnectionError(message, host=host, operation=_CAPTURE_OPERATION)
    raise InternalError(
        f"{message} (request {ctx_request_id})", host=host, operation=_CAPTURE_OPERATION
    )
