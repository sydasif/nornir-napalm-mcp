"""Re-export for backwards compatibility — moved to core/errors.py."""

from nornir_mcp.core.errors import (
    BackupError,
    CommandRejectedError,
    ConfigurationError,
    DeviceConnectionError,
    DeviceTimeoutError,
    ErrorType,
    InternalError,
    InventoryError,
    McpError,
    UnsupportedOperationError,
    ValidationError,
)

__all__ = [
    "BackupError",
    "CommandRejectedError",
    "ConfigurationError",
    "DeviceConnectionError",
    "DeviceTimeoutError",
    "ErrorType",
    "InternalError",
    "InventoryError",
    "McpError",
    "UnsupportedOperationError",
    "ValidationError",
]
