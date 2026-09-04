"""Core kernel — shared stateless services and the CoreBase class.

Nothing above ``core`` may import back into server.py or cli/.
"""

from __future__ import annotations

from nornir_mcp.core.audit import AuditLogger, get_audit_logger, reset_audit_logger
from nornir_mcp.core.capability import NETMIKO_DEVICE_TYPES, netmiko_device_type, supports_cli
from nornir_mcp.core.envelope import (
    HostOutcome,
    StructuredError,
    ToolEnvelope,
    maybe_truncate,
    outcome_from_mcp_error,
)
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
from nornir_mcp.core.policy import (
    EOS_RULESET,
    IOS_RULESET,
    MAX_COMMAND_LENGTH,
    CommandCategory,
    PlatformRuleset,
    PolicyViolation,
    assert_read_allowed,
    canonicalize,
    classify,
    validate_config_lines,
)
from nornir_mcp.core.runner import (
    EXECUTION_LOCK,
    NornirLike,
    execution_lock,
    get_nornir,
    reset_nornir,
)
from nornir_mcp.core.storage import (
    BackupRecord,
    BackupStore,
    FilesystemBackupStore,
    get_backup_store,
    reset_backup_store,
)
from nornir_mcp.core.tasks import _filter_devices, _results_to_outcomes, run_nornir_task

__all__ = [
    # runner
    "EXECUTION_LOCK",
    "NornirLike",
    "execution_lock",
    "get_nornir",
    "reset_nornir",
    # tasks
    "_filter_devices",
    "_results_to_outcomes",
    "run_nornir_task",
    # policy
    "EOS_RULESET",
    "IOS_RULESET",
    "CommandCategory",
    "MAX_COMMAND_LENGTH",
    "PlatformRuleset",
    "PolicyViolation",
    "assert_read_allowed",
    "canonicalize",
    "classify",
    "validate_config_lines",
    # capability
    "NETMIKO_DEVICE_TYPES",
    "netmiko_device_type",
    "supports_cli",
    # storage
    "BackupRecord",
    "BackupStore",
    "FilesystemBackupStore",
    "get_backup_store",
    "reset_backup_store",
    # audit
    "AuditLogger",
    "get_audit_logger",
    "reset_audit_logger",
    # errors
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
    # envelope
    "HostOutcome",
    "StructuredError",
    "ToolEnvelope",
    "maybe_truncate",
    "outcome_from_mcp_error",
]
