"""Re-export for backwards compatibility — moved to core/envelope.py."""

from nornir_mcp.core.envelope import (
    HostOutcome,
    StructuredError,
    ToolEnvelope,
    maybe_truncate,
    outcome_from_mcp_error,
)

__all__ = [
    "HostOutcome",
    "StructuredError",
    "ToolEnvelope",
    "maybe_truncate",
    "outcome_from_mcp_error",
]
