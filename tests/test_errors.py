"""Tests for nornir_mcp.errors — categorized errors and retry policy.

Spec §22 requires every tool-facing failure to carry a categorized error
type, message, target device, operation, and retryable flag. Spec §23
defines the conservative retry policy these tests pin.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from nornir_mcp import errors
from nornir_mcp.errors import (
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

# (error class, expected error_type, expected retryable default)
RETRYABLE_CASES: list[tuple[type[McpError], ErrorType, bool]] = [
    (ValidationError, ErrorType.VALIDATION, False),
    (InventoryError, ErrorType.INVENTORY, False),
    (DeviceConnectionError, ErrorType.CONNECTION, True),
    (DeviceTimeoutError, ErrorType.TIMEOUT, True),
    (CommandRejectedError, ErrorType.COMMAND_REJECTED, False),
    (UnsupportedOperationError, ErrorType.UNSUPPORTED_OPERATION, False),
    (BackupError, ErrorType.BACKUP, False),
    (ConfigurationError, ErrorType.CONFIGURATION, False),
    (InternalError, ErrorType.INTERNAL, False),
]


# ---------------------------------------------------------------------------
# to_dict() shape (§22)
# ---------------------------------------------------------------------------


def test_to_dict_shape_includes_all_keys() -> None:
    """Verify to_dict emits the full §22 JSON shape."""
    exc = DeviceConnectionError(
        "SSH connection timed out",
        host="spine-01",
        operation="nornir_get_facts",
    )
    payload = exc.to_dict()
    assert set(payload) == {"type", "message", "host", "operation", "retryable"}
    assert payload == {
        "type": "connection",
        "message": "SSH connection timed out",
        "host": "spine-01",
        "operation": "nornir_get_facts",
        "retryable": True,
    }


def test_to_dict_with_optional_fields_unset() -> None:
    """Verify host/operation serialize as None when not provided."""
    payload = InternalError("boom").to_dict()
    assert payload["host"] is None
    assert payload["operation"] is None


def test_error_type_catalog_values() -> None:
    """Pin the exact ErrorType catalog to the spec's value strings."""
    assert {e.value for e in ErrorType} == {
        "validation",
        "inventory",
        "connection",
        "authentication",
        "timeout",
        "command_rejected",
        "unsupported_operation",
        "configuration",
        "backup",
        "internal",
    }


# ---------------------------------------------------------------------------
# Retryable defaults per class (§23)
# ---------------------------------------------------------------------------


def test_retryable_defaults_per_class() -> None:
    """Connection/timeout are retryable; everything else is not."""
    for cls, expected_type, expected_retryable in RETRYABLE_CASES:
        exc = cls("boom")
        assert exc.error_type is expected_type, f"{cls.__name__} error_type"
        assert exc.retryable is expected_retryable, f"{cls.__name__} retryable"


# ---------------------------------------------------------------------------
# McpError as a plain exception
# ---------------------------------------------------------------------------


def test_mcp_error_works_as_plain_exception() -> None:
    """McpError is a normal Exception: message, defaults, catchable."""
    exc = McpError("boom")
    assert isinstance(exc, Exception)
    assert str(exc) == "boom"
    assert exc.host is None
    assert exc.operation is None
    assert exc.retryable is False
    assert exc.error_type is ErrorType.INTERNAL

    with pytest.raises(McpError):
        raise DeviceTimeoutError("device unreachable", host="leaf-01")


def test_base_error_accepts_explicit_type_and_retryable() -> None:
    """McpError itself can carry any category (e.g. authentication)."""
    exc = McpError(
        "authentication failed",
        host="spine-01",
        error_type=ErrorType.AUTHENTICATION,
        retryable=False,
    )
    assert exc.error_type is ErrorType.AUTHENTICATION
    assert exc.retryable is False
    assert exc.to_dict()["type"] == "authentication"


# ---------------------------------------------------------------------------
# Dependency purity
# ---------------------------------------------------------------------------


def test_errors_module_does_not_import_nornir() -> None:
    """errors.py must stay pure stdlib + enum (no nornir import)."""
    code = (
        "import sys\n"
        "import nornir_mcp.errors\n"
        "imported = [m for m in sys.modules if m == 'nornir' or m.startswith('nornir.')]\n"
        "assert not imported, f'errors.py transitively imports nornir: {imported}'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_errors_module_has_expected_exports() -> None:
    """The module exposes the documented error classes."""
    for name in (
        "ErrorType",
        "McpError",
        "ValidationError",
        "InventoryError",
        "DeviceConnectionError",
        "DeviceTimeoutError",
        "CommandRejectedError",
        "UnsupportedOperationError",
        "BackupError",
        "ConfigurationError",
        "InternalError",
    ):
        assert hasattr(errors, name), name
