"""Tests for storage.py — immutable per-host backup storage."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nornir_mcp import storage
from nornir_mcp.errors import ValidationError
from nornir_mcp.storage import FilesystemBackupStore

CONTENT = "hostname spine-01\ninterface Ethernet1\n"


def _store(tmp_path: Path) -> FilesystemBackupStore:
    return FilesystemBackupStore(tmp_path / "backups")


def test_backup_save_and_list_roundtrip(tmp_path: Path) -> None:
    """Saved content round-trips through save() and list()."""
    store = _store(tmp_path)
    record = store.save("spine-01", CONTENT, trigger="standalone")
    assert record.host == "spine-01"
    assert record.trigger == "standalone"
    assert record.change_id is None
    assert record.backup_id
    assert Path(record.path).read_text("utf-8") == CONTENT

    assert store.list("spine-01") == [record]

    # A pre-change backup carries the change_id and is listed newest-last.
    pre = store.save("spine-01", CONTENT, trigger="pre_change", change_id="chg-1")
    assert pre.change_id == "chg-1"
    assert [r.change_id for r in store.list("spine-01")] == [None, "chg-1"]


def test_backup_metadata_sidecar_contents(tmp_path: Path) -> None:
    """The .meta.json sidecar records the content hash and size."""
    store = _store(tmp_path)
    record = store.save("spine-01", CONTENT, trigger="standalone")
    meta_path = Path(record.path).with_suffix(".meta.json")
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text("utf-8"))

    expected_sha = hashlib.sha256(CONTENT.encode("utf-8")).hexdigest()
    assert meta["backup_id"] == record.backup_id
    assert meta["host"] == "spine-01"
    assert meta["sha256"] == expected_sha
    assert meta["size"] == len(CONTENT.encode("utf-8"))
    assert meta["path"] == record.path
    assert meta["trigger"] == "standalone"
    assert "T" in meta["timestamp"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions not available")
def test_backup_files_are_0600_and_dirs_0700(tmp_path: Path) -> None:
    """Backup files are 0600 and host dirs are 0700."""
    store = _store(tmp_path)
    record = store.save("spine-01", CONTENT, trigger="standalone")
    cfg_mode = stat.S_IMODE(os.stat(record.path).st_mode)
    meta_mode = stat.S_IMODE(os.stat(Path(record.path).with_suffix(".meta.json")).st_mode)
    dir_mode = stat.S_IMODE(os.stat(Path(record.path).parent).st_mode)
    assert cfg_mode == 0o600
    assert meta_mode == 0o600
    assert dir_mode == 0o700


@pytest.mark.parametrize("bad", ["..", "../etc", "a/b", "/etc/passwd", ".", "spine-01/x"])
def test_host_path_traversal_rejected(tmp_path: Path, bad: str) -> None:
    """Unsafe host names cannot construct paths outside the host dir."""
    store = _store(tmp_path)
    with pytest.raises(ValidationError):
        store.save(bad, CONTENT, trigger="standalone")
    with pytest.raises(ValidationError):
        store.list(bad)


def test_backups_are_immutable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing backup file is never overwritten — the save is refused."""
    store = _store(tmp_path)
    frozen = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    monkeypatch.setattr(storage, "_now_utc", lambda: frozen)

    first = store.save("spine-01", CONTENT, trigger="standalone")
    with pytest.raises(ValidationError, match="immutable"):
        store.save("spine-01", CONTENT, trigger="standalone")
    # The original backup is untouched.
    assert Path(first.path).read_text("utf-8") == CONTENT
    assert len(store.list("spine-01")) == 1


def test_same_second_saves_do_not_collide(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Microsecond-precision ids keep same-second saves distinct."""
    store = _store(tmp_path)
    base = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    micros = iter([100000, 200000])
    monkeypatch.setattr(storage, "_now_utc", lambda: base.replace(microsecond=next(micros)))

    a = store.save("spine-01", CONTENT, trigger="standalone")
    b = store.save("spine-01", "other config", trigger="standalone")
    assert a.backup_id != b.backup_id
    assert Path(a.path).exists()
    assert Path(b.path).exists()
    assert len(store.list("spine-01")) == 2
