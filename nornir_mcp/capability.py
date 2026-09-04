"""Re-export for backwards compatibility — moved to core/capability.py."""

from nornir_mcp.core.capability import (
    NETMIKO_DEVICE_TYPES,
    netmiko_device_type,
    supports_cli,
)

__all__ = [
    "NETMIKO_DEVICE_TYPES",
    "netmiko_device_type",
    "supports_cli",
]
