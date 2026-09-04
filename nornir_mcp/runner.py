"""Re-export for backwards compatibility — moved to core/runner.py."""

from nornir_mcp.core.runner import (
    EXECUTION_LOCK,
    NornirLike,
    execution_lock,
    get_nornir,
    reset_nornir,
)

__all__ = [
    "EXECUTION_LOCK",
    "NornirLike",
    "execution_lock",
    "get_nornir",
    "reset_nornir",
]
