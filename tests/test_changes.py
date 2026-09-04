"""Tests for changes.py — the orchestration heart of the write path.

Covers planning (per-host policy + capability), fail-closed pre-change
backups (spec §8.2: if a backup fails the device is NOT touched), and the
§16.1 dry-run outcome labeling ("planned_commands_only").
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from nornir_mcp.core.errors import BackupError, DeviceConnectionError
from nornir_mcp.core.storage import FilesystemBackupStore
from nornir_mcp.tools.netmiko.changes import (
    ChangePlan,
    capture_pre_change_backups,
    dry_run_outcomes,
    parse_config_transcript,
    plan_change,
)


def test_plan_change_collects_per_host_violations() -> None:
    """DANGEROUS lines become per-host violations, keyed by host."""
    plan = plan_change(
        {"spine-01": "ios", "leaf-01": "eos"},
        ["interface Ethernet1", "reload"],
    )
    assert set(plan.violations) == {"spine-01", "leaf-01"}
    for host in plan.hosts:
        assert len(plan.violations[host]) == 1
        assert plan.violations[host][0].category == "dangerous"
        assert "line 2" in plan.violations[host][0].reason
    assert plan.capability_errors == {}


def test_plan_change_clean_lines_have_no_violations() -> None:
    """CONFIGURATION/UNKNOWN lines pass the plan untouched."""
    plan = plan_change(
        {"spine-01": "ios", "leaf-01": "eos"},
        ["interface Ethernet1", "description uplink"],
    )
    assert plan.violations == {}
    assert plan.capability_errors == {}


def test_plan_change_flags_unknown_platform_hosts() -> None:
    """Platforms without ruleset/netmiko support land in capability_errors."""
    plan = plan_change(
        {"spine-01": "ios", "leaf-01": "junos"},
        ["interface Ethernet1"],
    )
    assert set(plan.capability_errors) == {"leaf-01"}
    assert "junos" in plan.capability_errors["leaf-01"]
    # The unsupported host is not line-validated (capability takes priority).
    assert plan.violations == {}


def test_plan_change_generates_change_id() -> None:
    """change_id has the chg-<12 hex> shape."""
    plan = plan_change({"spine-01": "ios"}, ["interface Ethernet1"])
    assert re.fullmatch(r"chg-[0-9a-f]{12}", plan.change_id)
    other = plan_change({"spine-01": "ios"}, ["interface Ethernet1"])
    assert other.change_id != plan.change_id


def test_change_plan_model_shape() -> None:
    """ChangePlan carries the fields the apply tool will consume."""
    plan = ChangePlan(
        change_id="chg-test",
        hosts={"spine-01": "ios"},
        lines=["interface Ethernet1"],
        violations={},
        capability_errors={},
    )
    assert plan.change_id == "chg-test"
    assert plan.hosts == {"spine-01": "ios"}
    assert plan.lines == ["interface Ethernet1"]
    assert plan.violations == {}
    assert plan.capability_errors == {}


def test_plan_change_is_pure() -> None:
    """plan_change never touches devices: hosts/lines are copied."""
    hosts = {"spine-01": "ios"}
    lines = ["interface Ethernet1"]
    plan = plan_change(hosts, lines)
    hosts["hacked"] = "ios"
    lines.append("reload")
    assert plan.hosts == {"spine-01": "ios"}
    assert plan.lines == ["interface Ethernet1"]


def test_pre_change_backup_stores_record_for_planned_hosts(tmp_path: Path) -> None:
    """Passing hosts get immutable pre_change backups tied to the change."""
    store = FilesystemBackupStore(tmp_path)
    plan = plan_change(
        {"spine-01": "ios", "leaf-01": "eos"},
        ["interface Ethernet1", "description uplink"],
    )
    captured: list[str] = []

    def fake_capture(host: str, platform: str, ctx_request_id: str) -> str:
        captured.append(host)
        assert ctx_request_id == plan.change_id
        return f"running config of {host}"

    records = capture_pre_change_backups(plan, capture=fake_capture, store=store)
    assert set(records) == {"spine-01", "leaf-01"}
    assert captured == ["spine-01", "leaf-01"]
    for host, record in records.items():
        assert record.trigger == "pre_change"
        assert record.change_id == plan.change_id
        assert record.sha256 == hashlib.sha256(f"running config of {host}".encode()).hexdigest()
        assert store.list(host) == [record]


def test_pre_change_backup_failure_raises_backup_error_fail_closed(
    tmp_path: Path,
) -> None:
    """One failing host raises BackupError; earlier saves are harmless extra state."""
    store = FilesystemBackupStore(tmp_path)
    plan = plan_change(
        {"spine-01": "ios", "leaf-01": "ios"},
        ["interface Ethernet1"],
    )

    def failing_capture(host: str, platform: str, ctx_request_id: str) -> str:
        if host == "leaf-01":
            raise DeviceConnectionError("ssh timeout", host=host, operation="config_capture")
        return f"running config of {host}"

    with pytest.raises(BackupError) as excinfo:
        capture_pre_change_backups(plan, capture=failing_capture, store=store)
    message = str(excinfo.value)
    assert "leaf-01" in message
    assert "NOT touched" in message or "not touched" in message
    # spine-01's immutable backup remains — harmless extra state (§8.2).
    assert len(store.list("spine-01")) == 1
    assert store.list("spine-01")[0].trigger == "pre_change"
    assert store.list("leaf-01") == []


def test_pre_change_backup_failure_occurs_before_any_device_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Capture-only: nothing that writes to devices is ever invoked."""
    write_calls: list[str] = []
    # Sentinel: the future apply path would use netmiko_send_config; this
    # step must never reach it.
    monkeypatch.setattr(
        "nornir_mcp.tools.netmiko.tool.netmiko_send_config",
        lambda *args: write_calls.append("netmiko_send_config"),
        raising=False,
    )
    store = FilesystemBackupStore(tmp_path)
    plan = plan_change({"spine-01": "ios", "leaf-01": "ios"}, ["interface Ethernet1"])
    captured: list[str] = []

    def failing_capture(host: str, platform: str, ctx_request_id: str) -> str:
        captured.append(host)
        if host == "leaf-01":
            raise DeviceConnectionError("ssh timeout", host=host, operation="config_capture")
        return f"running config of {host}"

    with pytest.raises(BackupError):
        capture_pre_change_backups(plan, capture=failing_capture, store=store)
    # Captures proceeded in order up to the failure; the write sentinel
    # was never called; the exception propagated to the caller.
    assert captured == ["spine-01", "leaf-01"]
    assert write_calls == []


def test_dry_run_outcomes_labels_planned_commands_only() -> None:
    """§16.1: passing hosts carry the mandatory dry_run_mode label."""
    plan = plan_change(
        {"spine-01": "ios", "leaf-01": "eos"},
        ["interface Ethernet1", "description uplink"],
    )
    outcomes = dry_run_outcomes(plan, request_id="req-abc")
    assert set(outcomes) == {"spine-01", "leaf-01"}
    for host in plan.hosts:
        outcome = outcomes[host]
        assert outcome.success is True
        assert outcome.error is None
        assert outcome.data is not None
        assert outcome.data["dry_run_mode"] == "planned_commands_only"
        assert outcome.data["commands"] == ["interface Ethernet1", "description uplink"]
        assert outcome.data["policy_result"] == "pass"


def test_dry_run_outcomes_shows_violations_per_host() -> None:
    """Violation hosts get command_rejected outcomes; nothing is planned."""
    plan = plan_change(
        {"spine-01": "ios", "leaf-01": "eos"},
        ["interface Ethernet1", "reload"],
    )
    outcomes = dry_run_outcomes(plan, request_id="req-abc")
    for host in plan.hosts:
        outcome = outcomes[host]
        assert outcome.success is False
        assert outcome.error is not None
        assert outcome.error.type == "command_rejected"
        assert outcome.error.host == host
        assert "line 2" in (outcome.error.message or "")
        assert outcome.data is None


def test_dry_run_outcomes_capability_error_is_unsupported_operation() -> None:
    """Capability-failed hosts get unsupported_operation outcomes (D8)."""
    plan = plan_change({"spine-01": "ios", "leaf-01": "junos"}, ["interface Ethernet1"])
    outcomes = dry_run_outcomes(plan, request_id="req-abc")
    assert outcomes["spine-01"].success is True
    leaf = outcomes["leaf-01"]
    assert leaf.success is False
    assert leaf.error is not None
    assert leaf.error.type == "unsupported_operation"
    assert "junos" in (leaf.error.message or "")


def test_parse_config_transcript_clean() -> None:
    """A transcript without error patterns reports applied == lines (D10)."""
    lines = ["interface Ethernet1", "no shutdown"]
    parsed = parse_config_transcript("interface Ethernet1\nno shutdown\n", "eos", lines)
    assert parsed["applied"] == lines
    assert parsed["failed_at"] is None
    assert "no error patterns" in parsed["device_state"]


def test_parse_config_transcript_detects_ios_error() -> None:
    """IOS '% Invalid input' marks the apply as unknown, not success (§8.3)."""
    transcript = "interface Ethernet1\n% Invalid input detected at 'Ethernet2'"
    parsed = parse_config_transcript(transcript, "ios", ["interface Ethernet1"])
    assert parsed["applied"] is None
    assert parsed["failed_at"] is None
    assert parsed["device_state"] == "unknown"
    assert "% Invalid input" in parsed["transcript"]


def test_parse_config_transcript_detects_eos_error_case_insensitive() -> None:
    """EOS 'Error:'/'incomplete' match case-insensitively."""
    for transcript in ("ERROR: bad line", "interface Ethernet1\nincomplete command"):
        parsed = parse_config_transcript(transcript, "eos", ["interface Ethernet1"])
        assert parsed["applied"] is None
        assert parsed["device_state"] == "unknown"


def test_parse_config_transcript_excerpt_is_truncated() -> None:
    """The transcript excerpt is bounded by maybe_truncate's budget."""
    transcript = "Error: " + "x" * 200_000
    parsed = parse_config_transcript(transcript, "eos", ["interface Ethernet1"])
    assert parsed["applied"] is None
    assert len(parsed["transcript"]) < 100_000
