"""Re-export for backwards compatibility — moved to core/storage.py."""

from nornir_mcp.core.storage import (
    BackupRecord,
    BackupStore,
    FilesystemBackupStore,
    _now_utc,
    get_backup_store,
    reset_backup_store,
)

__all__ = [
    "BackupRecord",
    "BackupStore",
    "FilesystemBackupStore",
    "_now_utc",
    "get_backup_store",
    "reset_backup_store",
]
