"""Backup storage for the write path (spec §8.2, §9, §19).

Spec §8.2 makes a backup a hard precondition of any change: before a
configuration is applied, the pre-change state must be safely stored.
Backups here are:

- **Immutable** — saving never overwrites; each backup gets its own
  microsecond-timestamped filename and existing files are never touched.
- **Per-host directories** under a configurable root, with file mode
  ``0600`` and directory mode ``0700``.
- **Metadata sidecars** — every ``.cfg`` file has a sibling
  ``.meta.json`` containing the :class:`BackupRecord`.
- **Traversal-safe** — host names (and backup ids) must match
  ``^[A-Za-z0-9][A-Za-z0-9._-]*$``, which structurally rejects ``..``,
  path separators, and absolute paths (spec §19).

The process-wide store reads its root from ``NORNIR_MCP_BACKUP_DIR``
(default ``./backups``) and is cached like ``get_nornir``; call
:func:`reset_backup_store` to re-read the environment (tests do this).
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from nornir_mcp.core.errors import ValidationError

# Conservative identifier charset: no path separators, no leading dot, so
# traversal attempts like "..", "../etc", or "a/b" cannot construct paths.
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_DEFAULT_BACKUP_DIR = "backups"
_ENV_BACKUP_DIR = "NORNIR_MCP_BACKUP_DIR"


def _now_utc() -> datetime:
    """Current UTC time (isolated so tests can freeze the clock)."""
    return datetime.now(UTC)


class BackupRecord(BaseModel):
    """Metadata for one immutable backup."""

    model_config = ConfigDict(frozen=True)

    backup_id: str
    host: str
    timestamp: str  # UTC ISO-8601
    path: str
    trigger: Literal["standalone", "pre_change"]
    change_id: str | None = None
    sha256: str
    size: int


class BackupStore(Protocol):
    """Storage contract consumed by the write path tools."""

    def save(
        self,
        host: str,
        content: str,
        *,
        trigger: Literal["standalone", "pre_change"],
        change_id: str | None = None,
    ) -> BackupRecord: ...

    def list(self, host: str) -> list[BackupRecord]: ...


class FilesystemBackupStore:
    """Immutable, per-host backup storage rooted at *root*."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- identifier validation (spec §19) ----------------------------------

    @staticmethod
    def _validate_identifier(value: str) -> None:
        if not _SAFE_IDENTIFIER.fullmatch(value):
            raise ValidationError(
                f"invalid host/backup identifier '{value}': must match "
                r"^[A-Za-z0-9][A-Za-z0-9._-]*$ (no '..', separators, or absolute paths)"
            )

    # -- public API --------------------------------------------------------

    def save(
        self,
        host: str,
        content: str,
        *,
        trigger: Literal["standalone", "pre_change"],
        change_id: str | None = None,
    ) -> BackupRecord:
        """Persist an immutable backup of *content* for *host*.

        Args:
            host: Device name (validated against path traversal).
            content: Configuration text to back up.
            trigger: Why the backup was taken.
            change_id: Correlating change, when taken pre-change.

        Returns:
            The BackupRecord describing the stored backup.

        Raises:
            ValidationError: For unsafe host names, or if the target file
                already exists (backups are immutable — never overwritten).
        """
        self._validate_identifier(host)
        if trigger not in ("standalone", "pre_change"):
            raise ValidationError(f"invalid backup trigger '{trigger}'")

        content_bytes = content.encode("utf-8")
        now = _now_utc()
        backup_id = now.strftime("%Y-%m-%dT%H-%M-%S_%fZ")
        cfg_path = self.root / host / f"{backup_id}.cfg"
        meta_path = self.root / host / f"{backup_id}.meta.json"

        host_dir = cfg_path.parent
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        os.makedirs(host_dir, mode=0o700, exist_ok=True)

        record = BackupRecord(
            backup_id=backup_id,
            host=host,
            timestamp=now.isoformat(),
            path=str(cfg_path),
            trigger=trigger,
            change_id=change_id,
            sha256=hashlib.sha256(content_bytes).hexdigest(),
            size=len(content_bytes),
        )

        # O_EXCL enforces immutability: never open an existing file.
        self._write_new(cfg_path, content_bytes)
        self._write_new(meta_path, record.model_dump_json().encode("utf-8"))
        return record

    def list(self, host: str) -> list[BackupRecord]:
        """Return this host's backups, oldest first."""
        self._validate_identifier(host)
        host_dir = self.root / host
        if not host_dir.is_dir():
            return []
        records: list[BackupRecord] = []
        for meta_path in sorted(host_dir.glob("*.meta.json")):
            try:
                records.append(BackupRecord.model_validate_json(meta_path.read_text("utf-8")))
            except (ValueError, OSError):
                # A torn/corrupt sidecar must not hide the other backups.
                continue
        return records

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _write_new(path: Path, payload: bytes) -> None:
        """Create *path* exclusively with mode 0600; never overwrite."""
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ValidationError(
                f"refusing to overwrite existing backup file '{path}' — backups are immutable"
            ) from exc
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)


@cache
def get_backup_store() -> FilesystemBackupStore:
    """Return the process-wide backup store (env-configured, cached)."""
    root = Path(os.environ.get(_ENV_BACKUP_DIR, _DEFAULT_BACKUP_DIR))
    return FilesystemBackupStore(root)


def reset_backup_store() -> None:
    """Clear the cached backup store so the next call re-reads the env."""
    get_backup_store.cache_clear()


# Re-exported for parity with the get_nornir/reset_nornir pattern.
__all__ = [
    "BackupRecord",
    "BackupStore",
    "FilesystemBackupStore",
    "get_backup_store",
    "reset_backup_store",
]
