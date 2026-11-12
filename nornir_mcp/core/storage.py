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
from dataclasses import dataclass
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


def _validate_identifier(value: str) -> None:
    """Validate a host/backup identifier against path-traversal (spec §19)."""
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValidationError(
            f"invalid host/backup identifier '{value}': must match "
            r"^[A-Za-z0-9][A-Za-z0-9._-]*$ (no '..', separators, or absolute paths)"
        )


_ENV_BACKUP_DIR = "NORNIR_MCP_BACKUP_DIR"


def _now_utc() -> datetime:
    """Current UTC time (isolated so tests can freeze the clock)."""
    return datetime.now(UTC)


def _generate_backup_id(now: datetime) -> str:
    """Generate a microsecond-precision backup ID from a UTC timestamp."""
    return now.strftime("%Y-%m-%dT%H-%M-%S_%fZ")


def _calculate_sha256(content_bytes: bytes) -> str:
    """Return the SHA-256 hex digest of *content_bytes*."""
    return hashlib.sha256(content_bytes).hexdigest()


def _build_backup_record(
    host: str,
    now: datetime,
    trigger: Literal["standalone", "pre_change"],
    change_id: str | None,
    cfg_path: Path,
    sha256: str,
    size: int,
) -> BackupRecord:
    """Build a BackupRecord from pre-computed values (pure)."""
    return BackupRecord(
        backup_id=_generate_backup_id(now),
        host=host,
        timestamp=now.isoformat(),
        path=str(cfg_path),
        trigger=trigger,
        change_id=change_id,
        sha256=sha256,
        size=size,
    )


@dataclass(frozen=True)
class _PlannedBackup:
    """Pure description of a backup write: validation + paths, no I/O.

    Separating planning from writing lets tests freeze the clock and assert
    on the planned record without touching the filesystem.
    """

    host: str
    content_bytes: bytes
    now: datetime
    backup_id: str
    cfg_path: Path
    meta_path: Path
    record: BackupRecord
    sha256: str
    size: int


def _backup_cfg_path(root: Path, host: str, backup_id: str) -> Path:
    return root / host / f"{backup_id}.cfg"


def _backup_meta_path(root: Path, host: str, backup_id: str) -> Path:
    return root / host / f"{backup_id}.meta.json"


def plan_backup_write(
    host: str,
    content: str,
    *,
    trigger: Literal["standalone", "pre_change"],
    change_id: str | None = None,
    now: datetime | None = None,
    root: Path | None = None,
) -> _PlannedBackup:
    """Plan an immutable backup write from pure inputs (no I/O).

    Validates identifiers, derives paths, and builds the BackupRecord, but
    does not touch the filesystem. *now* defaults to the current UTC time;
    *root* defaults to ``NORNIR_MCP_BACKUP_DIR`` (or ``./backups``).
    """
    _validate_identifier(host)
    if trigger not in ("standalone", "pre_change"):
        raise ValidationError(f"invalid backup trigger '{trigger}'")

    content_bytes = content.encode("utf-8")
    now = now if now is not None else _now_utc()
    backup_id = _generate_backup_id(now)
    root = root if root is not None else Path(os.environ.get(_ENV_BACKUP_DIR, _DEFAULT_BACKUP_DIR))
    cfg_path = _backup_cfg_path(root, host, backup_id)
    meta_path = _backup_meta_path(root, host, backup_id)

    sha256 = _calculate_sha256(content_bytes)
    size = len(content_bytes)
    record = _build_backup_record(
        host=host,
        now=now,
        trigger=trigger,
        change_id=change_id,
        cfg_path=cfg_path,
        sha256=sha256,
        size=size,
    )
    return _PlannedBackup(
        host=host,
        content_bytes=content_bytes,
        now=now,
        backup_id=backup_id,
        cfg_path=cfg_path,
        meta_path=meta_path,
        record=record,
        sha256=sha256,
        size=size,
    )


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


def write_backup_files(plan: _PlannedBackup) -> BackupRecord:
    """Persist a planned backup: create dirs, then exclusive 0600 files.

    O_EXCL enforces immutability — an existing backup is never overwritten.
    """
    host_dir = plan.cfg_path.parent
    root = host_dir.parent
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.makedirs(host_dir, mode=0o700, exist_ok=True)

    _write_new(plan.cfg_path, plan.content_bytes)
    _write_new(plan.meta_path, plan.record.model_dump_json().encode("utf-8"))
    return plan.record


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
        _validate_identifier(value)

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
        # Pure plan: validation, ids, paths, record — no I/O yet.
        plan = plan_backup_write(
            host=host,
            content=content,
            trigger=trigger,
            change_id=change_id,
            root=self.root,
        )
        # Effect: create dirs, then write exclusive 0600 files (never overwrite).
        return write_backup_files(plan)

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
