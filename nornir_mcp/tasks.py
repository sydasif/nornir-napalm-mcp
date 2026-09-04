"""Re-export for backwards compatibility — moved to core/tasks.py."""

from nornir_mcp.core.tasks import (
    _filter_devices,
    _results_to_outcomes,
    netmiko_send_commands,
    run_nornir_task,
)

__all__ = [
    "_filter_devices",
    "_results_to_outcomes",
    "netmiko_send_commands",
    "run_nornir_task",
]
