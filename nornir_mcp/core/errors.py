"""Categorized errors for the MCP server (spec §22).

Every tool-facing failure is raised as an :class:`McpError` subclass that
carries the §22 fields:

- ``error_type`` — a categorical :class:`ErrorType`
- ``message`` — human-readable description
- ``host`` — target device name (or ``None`` if not device-scoped)
- ``operation`` — the tool/operation that failed (or ``None``)
- ``retryable`` — whether a caller may safely retry

Retry policy follows spec §23 — retries must be conservative:

- **Retryable**: connection failures and timeouts (transient transport
  issues, e.g. a temporary SSH/network failure).
- **Never retry**: authentication failures, command/policy rejection,
  configuration errors — and, by default, anything unclassified.

Naming note: the connection subclass is :class:`DeviceConnectionError`,
not ``ConnectionError``, to avoid shadowing the Python builtin
``ConnectionError``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, TypedDict


class ErrorPayload(TypedDict):
    """The §22 JSON shape produced by :meth:`McpError.to_dict`."""

    type: str
    message: str
    host: str | None
    operation: str | None
    retryable: bool


class ErrorType(StrEnum):
    """Categorical error types used on the wire (spec §22).

    A ``str``-backed enum so ``.value`` (and string conversion) yield the
    exact wire strings above.
    """

    VALIDATION = "validation"
    INVENTORY = "inventory"
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    TIMEOUT = "timeout"
    COMMAND_REJECTED = "command_rejected"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    CONFIGURATION = "configuration"
    BACKUP = "backup"
    INTERNAL = "internal"


class McpError(Exception):
    """Base class for all categorized MCP errors.

    Args:
        message: Human-readable error description.
        host: Target device name, or ``None`` if not device-scoped.
        operation: The tool/operation that failed, or ``None``.
        error_type: Overrides the subclass's preset category.
        retryable: Overrides the subclass's preset retry policy.
    """

    # Presets; subclasses override these. The base defaults to the most
    # conservative policy (internal / never retry).
    _error_type: ClassVar[ErrorType] = ErrorType.INTERNAL
    _retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        host: str | None = None,
        operation: str | None = None,
        *,
        error_type: ErrorType | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.host = host
        self.operation = operation
        self.error_type = error_type if error_type is not None else type(self)._error_type
        self.retryable = retryable if retryable is not None else type(self)._retryable

    def to_dict(self) -> ErrorPayload:
        """Serialize to the §22 JSON shape.

        Returns:
            Dict with keys ``type``, ``message``, ``host``, ``operation``,
            and ``retryable``. Safe for MCP responses: never contains
            credentials or connection details.
        """
        return {
            "type": self.error_type.value,
            "message": self.message,
            "host": self.host,
            "operation": self.operation,
            "retryable": self.retryable,
        }


class ValidationError(McpError):
    """Invalid input or arguments (spec §22). Never retried."""

    _error_type: ClassVar[ErrorType] = ErrorType.VALIDATION
    _retryable: ClassVar[bool] = False


class InventoryError(McpError):
    """Inventory loading or device lookup failure. Never retried."""

    _error_type: ClassVar[ErrorType] = ErrorType.INVENTORY
    _retryable: ClassVar[bool] = False


class DeviceConnectionError(McpError):
    """Device connection failure (SSH/network).

    Named ``DeviceConnectionError`` rather than ``ConnectionError`` to avoid
    shadowing the Python builtin. Retryable per spec §23 (transient
    transport failures).
    """

    _error_type: ClassVar[ErrorType] = ErrorType.CONNECTION
    _retryable: ClassVar[bool] = True


class DeviceTimeoutError(McpError):
    """Device request timed out. Retryable per spec §23."""

    _error_type: ClassVar[ErrorType] = ErrorType.TIMEOUT
    _retryable: ClassVar[bool] = True


class CommandRejectedError(McpError):
    """Command rejected by the device or policy. Never retried."""

    _error_type: ClassVar[ErrorType] = ErrorType.COMMAND_REJECTED
    _retryable: ClassVar[bool] = False


class UnsupportedOperationError(McpError):
    """Operation not supported by the device/platform. Never retried."""

    _error_type: ClassVar[ErrorType] = ErrorType.UNSUPPORTED_OPERATION
    _retryable: ClassVar[bool] = False


class BackupError(McpError):
    """Backup failed or backup data unavailable. Never retried."""

    _error_type: ClassVar[ErrorType] = ErrorType.BACKUP
    _retryable: ClassVar[bool] = False


class ConfigurationError(McpError):
    """Invalid configuration or config application failure. Never retried."""

    _error_type: ClassVar[ErrorType] = ErrorType.CONFIGURATION
    _retryable: ClassVar[bool] = False


class InternalError(McpError):
    """Unexpected internal failure. Default category; never retried."""

    _error_type: ClassVar[ErrorType] = ErrorType.INTERNAL
    _retryable: ClassVar[bool] = False
