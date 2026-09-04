"""Append-only audit log for the write path (spec §25, D9).

Every write-path operation appends one JSON line describing *what* ran —
operation, request id, correlation change id, target hosts, outcome — so
changes are reconstructable after the fact.

**Never log configuration content**: spec §25 and the credentials rule of
§22 forbid device config text in MCP responses and logs. Callers must pass
only hashes/sizes (or other non-sensitive metadata) via ``details`` — the
raw config that was applied lives only in the immutable backups written by
:mod:`nornir_mcp.storage`.

Identity is ``getpass.getuser()`` per D9: stdio MCP has no strong
authenticated user, so the local OS username is a weak best-effort marker
and is documented as such.

The process-wide logger reads its directory from ``NORNIR_MCP_AUDIT_DIR``
(default ``./audit``) and is cached like the backup store; call
:func:`reset_audit_logger` to re-read the environment.
"""

from __future__ import annotations

import getpass
import json
import os
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

# Log file name inside the audit directory.
_AUDIT_FILENAME = "audit.jsonl"

_DEFAULT_AUDIT_DIR = "audit"
_ENV_AUDIT_DIR = "NORNIR_MCP_AUDIT_DIR"


def _now_utc() -> datetime:
    """Current UTC time (isolated so tests can freeze the clock)."""
    return datetime.now(UTC)


class AuditLogger:
    """Append-only JSONL audit log rooted at *root*."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.log_path = root / _AUDIT_FILENAME

    def record(
        self,
        operation: str,
        request_id: str,
        *,
        change_id: str | None = None,
        hosts: list[str],
        result: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append one audit entry.

        Args:
            operation: The tool/operation that ran.
            request_id: Correlation id from the request envelope.
            change_id: Correlating change id (write path).
            hosts: Target device names.
            result: Outcome summary (e.g. ``"applied"``/``"rejected"``).
            details: Optional metadata. **Callers must pass only hashes and
                sizes here — never configuration text** (spec §25).
        """
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        entry = {
            "timestamp": _now_utc().isoformat(),
            "user": getpass.getuser(),
            "operation": operation,
            "request_id": request_id,
            "change_id": change_id,
            "hosts": hosts,
            "result": result,
            "details": details,
        }
        line = json.dumps(entry, sort_keys=False) + "\n"
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)


@cache
def get_audit_logger() -> AuditLogger:
    """Return the process-wide audit logger (env-configured, cached)."""
    root = Path(os.environ.get(_ENV_AUDIT_DIR, _DEFAULT_AUDIT_DIR))
    return AuditLogger(root)


def reset_audit_logger() -> None:
    """Clear the cached audit logger so the next call re-reads the env."""
    get_audit_logger.cache_clear()


__all__ = ["AuditLogger", "get_audit_logger", "reset_audit_logger"]
