"""Tests for audit.py — append-only JSONL audit log."""

from __future__ import annotations

import getpass
import json
from pathlib import Path

from nornir_mcp.audit import AuditLogger


def _logger(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit")


def test_audit_appends_jsonl_lines_with_user_and_timestamp(tmp_path: Path) -> None:
    """Each record is one JSONL line with auto-filled user and timestamp."""
    logger = _logger(tmp_path)
    logger.record("nornir_run_command", "req-1", hosts=["spine-01"], result="ok")
    logger.record(
        "nornir_apply_config",
        "req-2",
        change_id="chg-1",
        hosts=["spine-01", "leaf-01"],
        result="applied",
        details={"sha256": "abc123", "size": 10},
    )

    lines = logger.log_path.read_text("utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["timestamp"]
    assert first["user"] == getpass.getuser()
    assert first["operation"] == "nornir_run_command"
    assert first["request_id"] == "req-1"


def test_audit_record_shape_matches_spec_section_25(tmp_path: Path) -> None:
    """A record exposes exactly the §25 fields, with only metadata details."""
    logger = _logger(tmp_path)
    logger.record(
        "nornir_apply_config",
        "req-9",
        change_id="chg-1",
        hosts=["spine-01"],
        result="applied",
        details={"sha256": "deadbeef", "size": 42},
    )

    entry = json.loads(logger.log_path.read_text("utf-8"))
    assert set(entry) == {
        "timestamp",
        "user",
        "operation",
        "request_id",
        "change_id",
        "hosts",
        "result",
        "details",
    }
    assert entry["operation"] == "nornir_apply_config"
    assert entry["request_id"] == "req-9"
    assert entry["change_id"] == "chg-1"
    assert entry["hosts"] == ["spine-01"]
    assert entry["result"] == "applied"
    assert entry["details"] == {"sha256": "deadbeef", "size": 42}
