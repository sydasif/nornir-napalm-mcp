"""Re-export for backwards compatibility — moved to core/audit.py."""

from nornir_mcp.core.audit import AuditLogger, get_audit_logger, reset_audit_logger

__all__ = ["AuditLogger", "get_audit_logger", "reset_audit_logger"]
