"""Tests for Netmiko tools — run_command, run_commands, apply_config, save_config."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from nornir_mcp import server
from nornir_mcp.core import audit, runner, storage
from nornir_mcp.core.errors import BackupError
from tests.conftest import (
    FakeHost,
    FakeHosts,
    FakeInventory,
    FakeNornir,
    FakeTaskResult,
    NetmikoTaskShim,
    netmiko_config_transcripts,
)


def _ctx() -> Any:
    """A fake fastmcp Context carrying a stable request_id."""
    from types import SimpleNamespace

    return SimpleNamespace(request_id="test-request-id")


@pytest.fixture(autouse=True)
def _reload_server(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Reset runner's cached Nornir singleton before each test."""
    request.getfixturevalue("fake_nornir")
    runner.reset_nornir()
    yield
    runner.reset_nornir()


@pytest.fixture(autouse=True)
def _isolated_backup_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Point backup/audit storage at tmp dirs and reset their singletons."""
    monkeypatch.setenv("NORNIR_MCP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("NORNIR_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    storage.reset_backup_store()
    audit.reset_audit_logger()
    yield
    storage.reset_backup_store()
    audit.reset_audit_logger()


# ---------------------------------------------------------------------------
# nornir_run_command
# ---------------------------------------------------------------------------


def _patch_hosts(monkeypatch: pytest.MonkeyPatch, hosts_data: dict[str, FakeHost]) -> None:
    """Point runner.InitNornir at a bespoke fake inventory."""

    def mock_init(**_: object) -> FakeNornir:
        return FakeNornir(FakeInventory(FakeHosts(hosts_data)))

    monkeypatch.setattr("nornir_mcp.core.runner.InitNornir", mock_init)
    runner.reset_nornir()


def test_run_command_show_returns_output_envelope(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """A READ_ONLY command returns a truncation-shaped data envelope."""
    env = server._netmiko_tools.nornir_run_command(
        command="show version", name="spine-01", ctx=_ctx()
    )
    assert env.success is True
    assert env.error is None
    outcome = env.results["spine-01"]
    assert outcome.success is True
    assert outcome.error is None
    canned = "canned output [spine-01]: show version"
    assert outcome.data == {
        "command": "show version",
        "output": canned,
        "truncated": False,
        "original_size": len(canned),
    }
    # The original command string is what was sent.
    assert len(netmiko_fakes) == 1
    assert netmiko_fakes[0]["command_string"] == "show version"
    assert netmiko_fakes[0]["host"] == "spine-01"


def test_run_command_reload_rejected_per_host_and_not_executed(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """A DANGEROUS command is rejected per host; nothing reaches the device."""
    env = server._netmiko_tools.nornir_run_command(command="reload", name="spine-01", ctx=_ctx())
    assert env.success is False
    outcome = env.results["spine-01"]
    assert outcome.success is False
    assert outcome.error is not None
    assert outcome.error.type == "command_rejected"
    assert "dangerous" in outcome.error.message
    assert outcome.error.host == "spine-01"
    assert netmiko_fakes == []  # zero invocations — never executed


def test_run_command_newline_rejected_at_request_level(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """Multi-line commands fail at request level before any host gating."""
    env = server._netmiko_tools.nornir_run_command(command="show version\nreload", ctx=_ctx())
    assert env.success is False
    assert env.results == {}
    assert env.error is not None
    assert env.error.type == "validation"
    assert netmiko_fakes == []


def test_run_command_unknown_platform_capability_error(
    monkeypatch: pytest.MonkeyPatch, netmiko_fakes: list[dict[str, Any]]
) -> None:
    """Unsupported platforms fail per host with a capability error."""
    hosts = {"mx-01": FakeHost(name="mx-01", hostname="10.0.0.9", platform="junos", groups=[])}
    _patch_hosts(monkeypatch, hosts)
    env = server._netmiko_tools.nornir_run_command(
        command="show version", name="mx-01", ctx=_ctx()
    )
    assert env.success is False
    outcome = env.results["mx-01"]
    assert outcome.success is False
    assert outcome.error is not None
    assert outcome.error.type == "unsupported_operation"
    assert outcome.error.retryable is False
    assert "junos" in outcome.error.message
    assert "ios" in outcome.error.message and "eos" in outcome.error.message
    assert netmiko_fakes == []  # capability gate precedes any execution


def test_run_command_output_truncation_flags(
    monkeypatch: pytest.MonkeyPatch, netmiko_fakes: list[dict[str, Any]]
) -> None:
    """Outputs over the §21.1 byte budget are truncated with explicit flags."""
    monkeypatch.setenv("NORNIR_MCP_MAX_OUTPUT_BYTES", "16")
    env = server._netmiko_tools.nornir_run_command(
        command="show version", name="spine-01", ctx=_ctx()
    )
    data = env.results["spine-01"].data
    assert data is not None
    canned = "canned output [spine-01]: show version"
    assert data["truncated"] is True
    assert data["original_size"] == len(canned)
    assert len(data["output"].encode("utf-8")) <= 16


def test_run_command_mixed_hosts_partial_success_preserved(
    monkeypatch: pytest.MonkeyPatch, netmiko_fakes: list[dict[str, Any]]
) -> None:
    """One host failing its gate does not mask another host's success (§21)."""
    hosts = {
        "spine-01": FakeHost(name="spine-01", hostname="10.0.0.1", platform="eos", groups=[]),
        "mx-01": FakeHost(name="mx-01", hostname="10.0.0.9", platform="junos", groups=[]),
    }
    _patch_hosts(monkeypatch, hosts)
    env = server._netmiko_tools.nornir_run_command(command="show version", ctx=_ctx())
    # Both outcomes preserved; only the passing host executed.
    assert env.success is False  # mx-01 failed its gate
    assert env.results["spine-01"].success is True
    assert env.results["mx-01"].success is False
    assert env.results["mx-01"].error is not None
    assert env.results["mx-01"].error.type == "unsupported_operation"
    assert [call["host"] for call in netmiko_fakes] == ["spine-01"]


# ---------------------------------------------------------------------------
# netmiko_send_commands — real task return-shape contract
# ---------------------------------------------------------------------------


def test_netmiko_send_commands_returns_per_command_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real task returns ``{command: output}``, never ``{host: {command: output}}``.

    This pins the shape the empty-output-on-real-devices regression (2d28511)
    broke: a result wrapped as ``{host.name: {command: output}}`` is not
    keyed by command, so every downstream ``data.get(command)`` misses and
    the tool returns empty output. The fake substitutes only the inner
    ``netmiko_send_command``, so the test exercises the real wrapper logic.
    """

    def fake_send(task: Any, command_string: str = "", **kwargs: Any) -> FakeTaskResult:
        return FakeTaskResult(result=f"out[{command_string}]")

    monkeypatch.setattr("nornir_mcp.tools.netmiko.tool.netmiko_send_command", fake_send)
    from nornir_mcp.tools.netmiko.tool import netmiko_send_commands

    task = NetmikoTaskShim(
        FakeHost(name="spine-01", hostname="10.0.0.1", platform="ios", groups=[])
    )
    result = netmiko_send_commands(task, commands=["show ver", "show ip int br"])
    assert result.result == {"show ver": "out[show ver]", "show ip int br": "out[show ip int br]"}
    # Regression guard: hostname must NOT leak into the result as a wrapper key.
    assert "spine-01" not in result.result


# ---------------------------------------------------------------------------
# nornir_apply_config (spec §8, §16)
# ---------------------------------------------------------------------------


def test_apply_dry_run_returns_planned_commands_only_and_touches_nothing(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """dry_run=True returns §16.1-labeled outcomes with ZERO device calls."""
    env = server._netmiko_tools.nornir_apply_config(
        ["interface Ethernet1", "description uplink"], ctx=_ctx()
    )
    assert env.success is True
    for host, outcome in env.results.items():
        assert outcome.success is True
        assert outcome.data is not None
        assert outcome.data["dry_run_mode"] == "planned_commands_only"
        assert outcome.data["commands"] == ["interface Ethernet1", "description uplink"]
        assert outcome.data["policy_result"] == "pass"
    # No device was touched: not even a capture/backup, let alone a config push.
    assert netmiko_fakes == []


def test_apply_dry_run_default_is_true() -> None:
    """dry_run defaults to True; there is NO backup flag (decision D5)."""
    sig = inspect.signature(server._netmiko_tools.nornir_apply_config)
    assert sig.parameters["dry_run"].default is True
    assert "backup" not in sig.parameters


def test_apply_dry_run_writes_audit_record() -> None:
    """Dry runs are audited with result 'dry_run' and a hash, never config text."""
    server._netmiko_tools.nornir_apply_config(["interface Ethernet1"], ctx=_ctx())
    lines = Path(audit.get_audit_logger().log_path).read_text().splitlines()
    apply_records = [json.loads(line) for line in lines if "nornir_apply_config" in line]
    assert len(apply_records) == 1
    record = apply_records[0]
    assert record["result"] == "dry_run"
    assert record["change_id"].startswith("chg-")
    assert record["details"]["sha256"] == hashlib.sha256(b"interface Ethernet1").hexdigest()
    assert "interface Ethernet1" not in lines[-1]


def test_apply_backup_failure_blocks_apply_devices_untouched(
    netmiko_fakes: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§31.1: a failed pre-change backup blocks apply; nothing reaches devices."""

    def failing_backups(plan: object, capture: Any = None, store: Any = None) -> None:
        raise BackupError("pre-change backup failed for 'spine-01' — apply aborted")

    monkeypatch.setattr(
        "nornir_mcp.tools.netmiko.tool.capture_pre_change_backups", failing_backups
    )
    env = server._netmiko_tools.nornir_apply_config(
        ["interface Ethernet1"], dry_run=False, ctx=_ctx()
    )
    assert env.success is False
    assert env.error is not None
    assert env.error.type == "backup"
    assert env.results == {}
    assert netmiko_fakes == []


def test_apply_success_clean_transcript(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """Clean transcripts report applied == the config lines, success True."""
    config = ["interface Ethernet1", "description uplink"]
    env = server._netmiko_tools.nornir_apply_config(config, dry_run=False, ctx=_ctx())
    assert env.success is True
    for host, outcome in env.results.items():
        assert outcome.success is True
        assert outcome.error is None
        assert outcome.data is not None
        assert outcome.data["applied"] == config
        assert outcome.data["failed_at"] is None
        assert "no error patterns" in outcome.data["device_state"]
        assert outcome.data["change_id"].startswith("chg-")
        assert outcome.data["backup_id"]
    config_calls = [c for c in netmiko_fakes if c["function"] == "netmiko_send_config"]
    assert len(config_calls) == 2
    for call in config_calls:
        assert call["config_commands"] == config


def test_apply_error_transcript_reports_failure_not_success(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """§8.3: a detected error pattern is NEVER reported as success."""
    netmiko_config_transcripts["spine-01"] = (
        "interface Ethernet1\n% Invalid input detected at 'Ethernet2'"
    )
    env = server._netmiko_tools.nornir_apply_config(
        ["interface Ethernet1"], dry_run=False, ctx=_ctx()
    )
    assert env.success is False
    spine = env.results["spine-01"]
    assert spine.success is False
    assert spine.error is not None
    assert spine.error.type == "configuration"
    assert spine.data is not None
    assert spine.data["applied"] is None
    assert spine.data["device_state"] == "unknown"
    assert "% Invalid input" in spine.data["transcript"]
    # The sibling host applied cleanly and is still a success.
    assert env.results["leaf-01"].success is True


def test_apply_partial_violations_flowed_through_per_host(
    netmiko_fakes: list[dict[str, Any]],
    fake_nornir: dict[str, FakeHost],
) -> None:
    """Policy violations block only the offending host; others still apply."""
    fake_nornir["spine-01"].platform = "ios"
    # 'format flash' is BLOCKED on ios but unknown (allowed) on eos.
    env = server._netmiko_tools.nornir_apply_config(
        ["interface Ethernet1", "format flash"], dry_run=False, ctx=_ctx()
    )
    assert env.success is False
    spine = env.results["spine-01"]
    assert spine.success is False
    assert spine.error is not None
    assert spine.error.type == "command_rejected"
    assert "format flash" in (spine.error.message or "")
    leaf = env.results["leaf-01"]
    assert leaf.success is True
    assert leaf.data is not None
    assert leaf.data["applied"] == ["interface Ethernet1", "format flash"]
    # Only the passing host received a send_config call.
    config_calls = [c for c in netmiko_fakes if c["function"] == "netmiko_send_config"]
    assert [c["host"] for c in config_calls] == ["leaf-01"]


def test_apply_writes_audit_record_with_change_id_and_hash_not_config_text(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """§25: audit carries change_id + sha256 of lines, never the lines."""
    config = ["interface Ethernet1", "description uplink"]
    env = server._netmiko_tools.nornir_apply_config(config, dry_run=False, ctx=_ctx())
    assert env.success is True
    raw = Path(audit.get_audit_logger().log_path).read_text().splitlines()
    apply_records = [line for line in raw if "nornir_apply_config" in line]
    assert len(apply_records) == 1
    record = json.loads(apply_records[0])
    assert record["result"] == "success"
    assert record["change_id"].startswith("chg-")
    assert set(record["hosts"]) == {"spine-01", "leaf-01"}
    expected_hash = hashlib.sha256("\n".join(config).encode()).hexdigest()
    assert record["details"]["sha256"] == expected_hash
    # The audit line itself must never contain the config text (§25).
    assert "interface Ethernet1" not in apply_records[0]
    assert "description uplink" not in apply_records[0]


def test_apply_envelope_invariant_mixed_hosts(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """§21: one failing host makes top-level success False (error stays None)."""
    netmiko_config_transcripts["spine-01"] = "Error: incomplete command"
    env = server._netmiko_tools.nornir_apply_config(
        ["interface Ethernet1"], dry_run=False, ctx=_ctx()
    )
    assert env.error is None
    assert env.success is False
    assert env.results["spine-01"].success is False
    assert env.results["leaf-01"].success is True


# ---------------------------------------------------------------------------
# nornir_save_config (spec §11)
# ---------------------------------------------------------------------------


def test_save_config_success_envelope(netmiko_fakes: list[dict[str, Any]]) -> None:
    """Both capable hosts save and report success."""
    env = server._netmiko_tools.nornir_save_config(ctx=_ctx())
    assert env.success is True
    assert env.error is None
    assert set(env.results) == {"spine-01", "leaf-01"}
    for outcome in env.results.values():
        assert outcome.success is True
    save_calls = [c for c in netmiko_fakes if c["function"] == "netmiko_save_config"]
    assert {c["host"] for c in save_calls} == {"spine-01", "leaf-01"}


def test_save_config_unknown_platform_capability_error(
    netmiko_fakes: list[dict[str, Any]],
    fake_nornir: dict[str, FakeHost],
) -> None:
    """Unsupported platforms get a per-host capability error; nothing runs there."""
    fake_nornir["leaf-01"].platform = "junos"
    env = server._netmiko_tools.nornir_save_config(ctx=_ctx())
    assert env.success is False
    leaf = env.results["leaf-01"]
    assert leaf.success is False
    assert leaf.error is not None
    assert leaf.error.type == "unsupported_operation"
    assert "junos" in (leaf.error.message or "")
    assert env.results["spine-01"].success is True
    save_calls = [c for c in netmiko_fakes if c["function"] == "netmiko_save_config"]
    assert [c["host"] for c in save_calls] == ["spine-01"]


def test_save_config_writes_audit_record(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """§11 saves are audit-logged (operation, hosts, result)."""
    env = server._netmiko_tools.nornir_save_config(ctx=_ctx())
    assert env.success is True
    raw = Path(audit.get_audit_logger().log_path).read_text().splitlines()
    save_records = [line for line in raw if "nornir_save_config" in line]
    assert len(save_records) == 1
    record = json.loads(save_records[0])
    assert record["result"] == "success"
    assert set(record["hosts"]) == {"spine-01", "leaf-01"}


def test_save_config_partial_failure_invariant(
    netmiko_fakes: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§21: one failing host makes top-level success False; the rest still save."""

    def fake_netmiko_save_config_flaky(
        task: object, cmd: str = "", confirm: bool = False, **kw: object
    ) -> FakeTaskResult:
        if getattr(task, "host").name == "leaf-01":
            return FakeTaskResult(
                result="connection dropped", failed=True, exception=RuntimeError("ssh reset")
            )
        return FakeTaskResult(result="saved", changed=True)

    import nornir_mcp.tools.netmiko.tool as netmiko_tool_module

    monkeypatch.setattr(netmiko_tool_module, "netmiko_save_config", fake_netmiko_save_config_flaky)
    env = server._netmiko_tools.nornir_save_config(ctx=_ctx())
    assert env.error is None
    assert env.success is False
    assert env.results["spine-01"].success is True
    leaf = env.results["leaf-01"]
    assert leaf.success is False
    assert leaf.error is not None
    assert leaf.error.type == "connection"
    assert leaf.error.retryable is True


def test_apply_does_not_save_config(netmiko_fakes: list[dict[str, Any]]) -> None:
    """Spec §11: apply never triggers the save path — saves are explicit-only."""
    env = server._netmiko_tools.nornir_apply_config(
        ["interface Ethernet1"], dry_run=False, ctx=_ctx()
    )
    assert env.success is True
    functions = [c["function"] for c in netmiko_fakes]
    assert "netmiko_save_config" not in functions
    assert "netmiko_send_config" in functions
