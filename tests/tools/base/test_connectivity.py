"""Tests for nornir_mcp.tools.base.connectivity."""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nornir_mcp import server
from nornir_mcp.core import audit, runner, storage
from nornir_mcp.core.envelope import HostOutcome, ToolEnvelope
from nornir_mcp.core.errors import ValidationError
from nornir_mcp.core.runner import EXECUTION_LOCK
from nornir_mcp.tools.base.connectivity import (
    build_ping_command,
    check_tcp_host,
    icmp_ping_host,
    validate_probe_target,
)
from tests.conftest import FakeHosts, FakeInventory, FakeNornir, _make_host


def test_validate_probe_target_accepts_ipv4() -> None:
    """validate_probe_target accepts IPv4 addresses."""
    assert validate_probe_target("192.168.1.1") == "192.168.1.1"
    assert validate_probe_target("  192.168.1.1  ") == "192.168.1.1"
    assert validate_probe_target("0.0.0.0") == "0.0.0.0"
    assert validate_probe_target("255.255.255.255") == "255.255.255.255"


def test_validate_probe_target_accepts_ipv6() -> None:
    """validate_probe_target accepts IPv6 addresses."""
    assert validate_probe_target("::1") == "::1"
    assert validate_probe_target("2001:db8::1") == "2001:db8::1"
    assert validate_probe_target("  2001:db8::1  ") == "2001:db8::1"


def test_validate_probe_target_accepts_conservative_hostname() -> None:
    """validate_probe_target accepts conservative DNS hostnames."""
    assert validate_probe_target("localhost") == "localhost"
    assert validate_probe_target("example.com") == "example.com"
    assert validate_probe_target("host-name.example.com") == "host-name.example.com"
    assert validate_probe_target("a.b.c") == "a.b.c"


def test_validate_probe_target_rejects_empty_values() -> None:
    """validate_probe_target rejects empty values."""
    with pytest.raises(ValidationError, match="Target cannot be empty"):
        validate_probe_target("")
    with pytest.raises(ValidationError, match="Target cannot be empty"):
        validate_probe_target("   ")


def test_validate_probe_target_rejects_spaces() -> None:
    """validate_probe_target rejects spaces inside the target."""
    with pytest.raises(ValidationError, match="Target cannot contain whitespace"):
        validate_probe_target("192.168.1.1 test")
    with pytest.raises(ValidationError, match="Target cannot contain whitespace"):
        validate_probe_target("192 168 1 1")


def test_validate_probe_target_rejects_newline() -> None:
    """validate_probe_target rejects newline and carriage return."""
    with pytest.raises(ValidationError, match="Target contains control characters"):
        validate_probe_target("192.168.1.1\n")
    with pytest.raises(ValidationError, match="Target contains control characters"):
        validate_probe_target("192.168.1.1\r")
    with pytest.raises(ValidationError, match="Target contains control characters"):
        validate_probe_target("\n192.168.1.1")


def test_validate_probe_target_rejects_semicolon() -> None:
    """validate_probe_target rejects semicolon."""
    with pytest.raises(ValidationError, match="Target contains forbidden characters"):
        validate_probe_target("192.168.1.1;")


def test_validate_probe_target_rejects_pipe() -> None:
    """validate_probe_target rejects pipe."""
    with pytest.raises(ValidationError, match="Target contains forbidden characters"):
        validate_probe_target("192.168.1.1|")


def test_validate_probe_target_rejects_shell_expansion() -> None:
    """validate_probe_target rejects shell expansion characters."""
    for char in ["$", "`", "\\", "*", "?", "[", "]", "(", ")", "{", "}", ",", "%", "!", "~", "#"]:
        with pytest.raises(ValidationError, match="Target contains forbidden characters"):
            validate_probe_target(f"192.168.1.1{char}")


def test_validate_probe_target_rejects_ipv6_zone_index() -> None:
    """validate_probe_target rejects IPv6 zone indexes."""
    with pytest.raises(ValidationError, match="IPv6 zone index is not allowed"):
        validate_probe_target("fe80::1%eth0")


def test_validate_probe_target_rejects_long_hostname() -> None:
    """validate_probe_target rejects hostnames longer than 253 characters."""
    long_hostname = "a" * 254 + ".com"
    with pytest.raises(ValidationError, match="Hostname too long"):
        validate_probe_target(long_hostname)


def test_validate_probe_target_rejects_leading_trailing_dot() -> None:
    """validate_probe_target rejects leading or trailing dots."""
    with pytest.raises(ValidationError, match="Hostname cannot start or end with a dot"):
        validate_probe_target(".example.com")
    with pytest.raises(ValidationError, match="Hostname cannot start or end with a dot"):
        validate_probe_target("example.com.")


def test_validate_probe_target_rejects_consecutive_dots() -> None:
    """validate_probe_target rejects consecutive dots."""
    with pytest.raises(ValidationError, match="Hostname cannot contain consecutive dots"):
        validate_probe_target("host..example.com")


def test_validate_probe_target_rejects_invalid_hostname_label() -> None:
    """validate_probe_target rejects invalid hostname labels."""
    with pytest.raises(ValidationError, match="Hostname label contains invalid characters"):
        validate_probe_target("host_name.example.com")  # underscore not allowed
    with pytest.raises(ValidationError, match="Hostname label too long"):
        validate_probe_target("a" * 64 + ".example.com")
    with pytest.raises(ValidationError, match="Hostname label cannot start or end with a hyphen"):
        validate_probe_target("-invalid.example.com")
    with pytest.raises(ValidationError, match="Hostname label cannot start or end with a hyphen"):
        validate_probe_target("invalid-.example.com")


def test_build_ping_command_returns_argv_list() -> None:
    """build_ping_command returns an argv list, never a shell string."""
    cmd = build_ping_command("example.com", 3000)
    assert isinstance(cmd, list)
    assert all(isinstance(part, str) for part in cmd)
    # Should not contain shell metacharacters or be a single string
    assert len(cmd) > 1
    assert "example.com" in cmd


def test_build_ping_command_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_ping_command returns Windows-appropriate command."""
    monkeypatch.setattr("platform.system", lambda: "Windows")
    cmd = build_ping_command("192.168.1.1", 1500)
    expected = ["ping", "-n", "1", "-w", "1500", "192.168.1.1"]
    assert cmd == expected


def test_build_ping_command_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_ping_command returns Linux-appropriate command."""
    monkeypatch.setattr("platform.system", lambda: "Linux")
    cmd = build_ping_command("192.168.1.1", 1500)
    # 1500 ms -> 2 seconds (rounded up, min 1)
    expected = ["ping", "-c", "1", "-W", "2", "192.168.1.1"]
    assert cmd == expected


def test_build_ping_command_linux_uses_ceil_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux ping timeout ceil()s milliseconds to whole seconds (min 1)."""
    monkeypatch.setattr("platform.system", lambda: "Linux")
    for timeout_ms, expected_sec in [(500, "1"), (1500, "2"), (2500, "3")]:
        cmd = build_ping_command("192.168.1.1", timeout_ms)
        assert cmd == ["ping", "-c", "1", "-W", expected_sec, "192.168.1.1"]


def test_build_ping_command_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_ping_command returns Darwin/Unix-appropriate command."""
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    cmd = build_ping_command("192.168.1.1", 1500)
    expected = ["ping", "-c", "1", "192.168.1.1"]
    assert cmd == expected


def test_icmp_ping_host_returns_latency_on_success() -> None:
    """icmp_ping_host returns reachable=True and latency_ms when ping succeeds."""

    def fake_runner() -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [],
            returncode=0,
            stdout="64 bytes from 8.8.8.8: icmp_seq=1 ttl=115 time=23.4 ms",
            stderr="",
        )

    reachable, latency_ms, error = icmp_ping_host("8.8.8.8", 1000, lambda _c, _t: fake_runner())
    assert reachable is True
    assert isinstance(latency_ms, float)
    assert latency_ms >= 0
    assert error is None


def test_icmp_ping_host_returns_failure_on_nonzero_exit() -> None:
    """icmp_ping_host returns reachable=False when ping fails."""

    def fake_runner() -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [], returncode=1, stdout="", stderr="Network is unreachable"
        )

    reachable, latency_ms, error = icmp_ping_host(
        "192.168.1.1", 1000, lambda _c, _t: fake_runner()
    )
    assert reachable is False
    assert latency_ms is None
    assert error is not None
    assert "Network is unreachable" in error


def test_icmp_ping_host_returns_failure_on_timeout() -> None:
    """icmp_ping_host returns reachable=False on subprocess timeout."""

    def fake_runner(_cmd: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(_cmd, _timeout)

    reachable, latency_ms, error = icmp_ping_host("192.168.1.1", 1000, fake_runner)
    assert reachable is False
    assert latency_ms is None
    assert error is not None
    assert "timed out" in error


def test_icmp_ping_host_returns_failure_on_missing_executable() -> None:
    """icmp_ping_host returns reachable=False when ping executable is missing."""

    def fake_runner(_cmd: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError()

    reachable, latency_ms, error = icmp_ping_host("192.168.1.1", 1000, fake_runner)
    assert reachable is False
    assert latency_ms is None
    assert error is not None
    assert "ping executable not found" in error


def test_icmp_ping_host_returns_failure_on_permission_error() -> None:
    """icmp_ping_host returns reachable=False when ping permission is denied."""

    def fake_runner(_cmd: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        raise PermissionError()

    reachable, latency_ms, error = icmp_ping_host("192.168.1.1", 1000, fake_runner)
    assert reachable is False
    assert latency_ms is None
    assert error is not None
    assert "permission denied" in error


def test_icmp_ping_host_returns_failure_on_os_error() -> None:
    """icmp_ping_host treats OSError as an expected, graceful failure."""

    def fake_runner(_cmd: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        raise OSError("operation not permitted")

    reachable, latency_ms, error = icmp_ping_host("192.168.1.1", 1000, fake_runner)
    assert reachable is False
    assert latency_ms is None
    assert error is not None
    assert "operation not permitted" in error


def test_icmp_ping_host_propagates_unexpected_exceptions() -> None:
    """Unexpected runner exceptions propagate instead of being swallowed."""

    def fake_runner(_cmd: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        icmp_ping_host("192.168.1.1", 1000, fake_runner)


def test_icmp_ping_host_allows_validation_error_to_propagate() -> None:
    """icmp_ping_host allows ValidationError to propagate for invalid target."""
    with pytest.raises(ValidationError):
        icmp_ping_host("", 1000)  # empty target

    with pytest.raises(ValidationError):
        icmp_ping_host("invalid host", 1000)  # space in target


# ---------------------------------------------------------------------------
# Tool-level tests (nornir_ping)
#
# These run against the fake Nornir inventory from conftest. icmp_ping_host is
# monkeypatched in nornir_mcp.tools.base.tool so no real ICMP traffic is
# generated. The same fixture style as tests/tools/base/test_tool.py is used.
# ---------------------------------------------------------------------------


def _ctx() -> Any:
    """A fake fastmcp Context carrying a stable request_id."""
    return SimpleNamespace(request_id="test-request-id")


def _ping_stub(
    result: tuple[bool, float | None, str | None] = (True, 1.0, None),
) -> tuple[Callable[[str, int], tuple[bool, float | None, str | None]], list[tuple[str, int]]]:
    """Return a fake ``icmp_ping_host`` (exact signature) and its call recorder."""
    calls: list[tuple[str, int]] = []

    def fake(hostname: str, timeout_ms: int) -> tuple[bool, float | None, str | None]:
        calls.append((hostname, timeout_ms))
        return result

    return fake, calls


def _tcp_stub(
    result: tuple[bool, float | None, str | None] = (True, 1.0, None),
) -> tuple[
    Callable[[str, int, int], tuple[bool, float | None, str | None]],
    list[tuple[str, int, int]],
]:
    """Return a fake ``check_tcp_host`` (exact signature) and its call recorder."""
    calls: list[tuple[str, int, int]] = []

    def fake(hostname: str, port: int, timeout_ms: int) -> tuple[bool, float | None, str | None]:
        calls.append((hostname, port, timeout_ms))
        return result

    return fake, calls


def monkeypatch_inventory(hosts_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the fake Nornir inventory with a custom host map."""

    def mock_init(**_: object) -> FakeNornir:
        return FakeNornir(FakeInventory(FakeHosts(hosts_data)))

    # String-based monkeypatch (mirrors conftest) so mypy does not flag the
    # re-export; reset_nornir clears the cached singleton.
    monkeypatch.setattr("nornir_mcp.core.runner.InitNornir", mock_init)
    runner.reset_nornir()


@pytest.fixture(autouse=True)
def _reload_server(request: pytest.FixtureRequest) -> Generator[None]:
    """Reset runner's cached Nornir singleton before each test."""
    request.getfixturevalue("fake_nornir")
    runner.reset_nornir()
    yield
    runner.reset_nornir()


@pytest.fixture(autouse=True)
def _isolated_backup_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Point backup/audit storage at tmp dirs and reset their singletons."""
    monkeypatch.setenv("NORNIR_MCP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("NORNIR_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    storage.reset_backup_store()
    audit.reset_audit_logger()
    yield
    storage.reset_backup_store()
    audit.reset_audit_logger()


def test_nornir_ping_reachable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable host returns success and structured data."""
    fake_ping, _ = _ping_stub((True, 23.4, None))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.icmp_ping_host", fake_ping)

    env = server._nornir_base.nornir_ping(name="spine-01", ctx=_ctx())
    assert env.success is True
    outcome = env.results["spine-01"]
    assert outcome.success is True
    data = outcome.data
    assert data is not None
    assert data["host"] == "spine-01"
    assert data["hostname"] == "192.168.1.1"
    assert data["mode"] == "icmp"
    assert data["reachable"] is True
    assert data["latency_ms"] == 23.4


def test_nornir_ping_data_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Result data includes host, hostname, mode, reachable, latency_ms."""
    fake_ping, _ = _ping_stub((True, 12.0, None))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.icmp_ping_host", fake_ping)

    env = server._nornir_base.nornir_ping(name="leaf-01", ctx=_ctx())
    data = env.results["leaf-01"].data
    assert data is not None
    assert set(data) == {"host", "hostname", "mode", "reachable", "latency_ms"}
    assert "error_detail" not in data
    assert data["mode"] == "icmp"
    assert data["reachable"] is True


def test_nornir_ping_unreachable_success_when_flag_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreachable host with unreachable_as_error=False succeeds with reachable=False."""
    fake_ping, _ = _ping_stub((False, None, "Network is unreachable"))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.icmp_ping_host", fake_ping)

    env = server._nornir_base.nornir_ping(name="spine-01", unreachable_as_error=False, ctx=_ctx())
    assert env.success is True
    outcome = env.results["spine-01"]
    assert outcome.success is True
    data = outcome.data
    assert data is not None
    assert data["reachable"] is False
    assert data["latency_ms"] is None
    assert data["error_detail"] == "Network is unreachable"


def test_nornir_ping_unreachable_error_when_flag_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreachable host with unreachable_as_error=True fails with connection error."""
    fake_ping, _ = _ping_stub((False, None, "Network is unreachable"))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.icmp_ping_host", fake_ping)

    env = server._nornir_base.nornir_ping(name="spine-01", unreachable_as_error=True, ctx=_ctx())
    assert env.success is False
    outcome = env.results["spine-01"]
    assert outcome.success is False
    assert outcome.error is not None
    assert outcome.error.type == "connection"
    assert outcome.error.retryable is True


def test_nornir_ping_invalid_timeout() -> None:
    """Invalid timeout_ms returns a request-level validation error."""
    env = server._nornir_base.nornir_ping(name="spine-01", timeout_ms=50, ctx=_ctx())
    assert env.success is False
    assert env.error is not None
    assert env.error.type == "validation"
    assert "timeout_ms" in env.error.message


def test_nornir_ping_no_matching_hosts() -> None:
    """No matching hosts returns a request-level validation error."""
    env = server._nornir_base.nornir_ping(name="nonexistent", ctx=_ctx())
    assert env.success is False
    assert env.error is not None
    assert env.error.type == "validation"


def test_nornir_ping_empty_name_list() -> None:
    """An explicitly empty name list returns a request-level validation error."""
    env = server._nornir_base.nornir_ping(name=[], ctx=_ctx())
    assert env.success is False
    assert env.error is not None
    assert env.error.type == "validation"


def test_nornir_ping_missing_hostname_fails_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host with empty inventory hostname produces a failed validation outcome."""
    fake_ping, calls = _ping_stub()
    monkeypatch.setattr("nornir_mcp.tools.base.tool.icmp_ping_host", fake_ping)
    hosts_data = {"empty-01": _make_host("empty-01", "", "eos", ["spine"])}
    monkeypatch_inventory(hosts_data, monkeypatch)

    env = server._nornir_base.nornir_ping(ctx=_ctx())
    outcome = env.results["empty-01"]
    assert outcome.success is False
    assert outcome.error is not None
    assert outcome.error.type == "validation"
    assert calls == []


def test_nornir_ping_audit_record_execution_based(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit record is written with execution-based result and no raw ping output."""
    fake_ping, _ = _ping_stub((True, 15.0, None))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.icmp_ping_host", fake_ping)

    env = server._nornir_base.nornir_ping(ctx=_ctx())
    assert env.success is True

    lines = audit.get_audit_logger().log_path.read_text("utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["operation"] == "nornir_ping"
    assert entry["request_id"] == "test-request-id"
    assert set(entry["hosts"]) == {"spine-01", "leaf-01"}
    assert entry["result"] == "success"
    details = entry["details"]
    assert details["mode"] == "icmp"
    assert details["reachable_count"] == 2
    assert details["unreachable_count"] == 0
    assert details["invalid_count"] == 0
    payload = json.dumps(entry)
    assert "Network is unreachable" not in payload


def test_nornir_ping_audit_details_exclude_raw_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit details never contain raw ping output."""
    fake_ping, _ = _ping_stub((False, None, "Raw ping stderr: unreachable"))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.icmp_ping_host", fake_ping)

    server._nornir_base.nornir_ping(ctx=_ctx())

    lines = audit.get_audit_logger().log_path.read_text("utf-8").strip().splitlines()
    entry = json.loads(lines[0])
    assert "Raw ping stderr" not in json.dumps(entry["details"])


def test_nornir_ping_audit_excludes_error_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit metadata never carries the error_detail of unreachable outcomes."""
    fake_ping, _ = _ping_stub((False, None, "ping command timed out"))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.icmp_ping_host", fake_ping)

    server._nornir_base.nornir_ping(ctx=_ctx())

    lines = audit.get_audit_logger().log_path.read_text("utf-8").strip().splitlines()
    entry = json.loads(lines[0])
    assert "ping command timed out" not in json.dumps(entry)
    assert entry["details"]["unreachable_count"] == 2


def test_nornir_ping_no_netmiko_invocation(
    monkeypatch: pytest.MonkeyPatch, netmiko_fakes: list[dict[str, Any]]
) -> None:
    """No Netmiko fake invocations occur during a ping."""
    fake_ping, _ = _ping_stub((True, 5.0, None))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.icmp_ping_host", fake_ping)

    server._nornir_base.nornir_ping(ctx=_ctx())
    assert netmiko_fakes == []


def test_nornir_ping_holds_execution_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """nornir_ping runs its selection and probe loop under the execution lock."""
    probe_thread_acquired: list[bool] = []

    def fake_ping(hostname: str, timeout_ms: int) -> tuple[bool, float | None, None]:
        # The tool thread already holds EXECUTION_LOCK. RLock is reentrant in
        # the same thread, so probe from a separate thread to prove the lock is
        # held while the probe runs.
        def _try_acquire() -> None:
            acquired = EXECUTION_LOCK.acquire(blocking=False)
            if acquired:
                EXECUTION_LOCK.release()
            probe_thread_acquired.append(acquired)

        thread = threading.Thread(target=_try_acquire)
        thread.start()
        thread.join()
        return (True, 1.0, None)

    monkeypatch.setattr("nornir_mcp.tools.base.tool.icmp_ping_host", fake_ping)

    env = server._nornir_base.nornir_ping(name="spine-01", ctx=_ctx())
    assert env.success is True
    assert probe_thread_acquired == [False]


def test_nornir_ping_unexpected_helper_error_isolated_per_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected icmp_ping_host exception fails that host, not the request."""

    def fake_ping(hostname: str, timeout_ms: int) -> tuple[bool, float | None, str | None]:
        raise RuntimeError("boom")

    monkeypatch.setattr("nornir_mcp.tools.base.tool.icmp_ping_host", fake_ping)

    env = server._nornir_base.nornir_ping(name="spine-01", ctx=_ctx())
    outcome = env.results["spine-01"]
    assert outcome.success is False
    assert outcome.error is not None
    assert outcome.error.type == "internal"
    assert outcome.error.retryable is False
    assert outcome.error.host == "spine-01"
    assert "ping diagnostic failed unexpectedly" in outcome.error.message
    assert "boom" in outcome.error.message


# ---------------------------------------------------------------------------
# check_tcp_host helper tests
# ---------------------------------------------------------------------------


class _FakeConn:
    """A fake socket that records whether close() was called."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_check_tcp_host_success_returns_latency() -> None:
    """A successful connector returns reachable=True and latency_ms."""

    def fake_connector(addr: tuple[str, int], timeout: float) -> _FakeConn:
        assert addr == ("192.168.1.1", 22)
        assert timeout == 3.0  # 3000 ms -> 3.0 s
        return _FakeConn()

    reachable, latency_ms, error = check_tcp_host("192.168.1.1", 22, 3000, fake_connector)
    assert reachable is True
    assert isinstance(latency_ms, float)
    assert latency_ms >= 0
    assert error is None


def test_check_tcp_host_failure_returns_error_detail() -> None:
    """A failing connector returns reachable=False with error_detail."""

    def fake_connector(addr: tuple[str, int], timeout: float) -> _FakeConn:
        raise OSError("Connection refused")

    reachable, latency_ms, error = check_tcp_host("192.168.1.1", 22, 3000, fake_connector)
    assert reachable is False
    assert latency_ms is None
    assert error is not None
    assert "Connection refused" in error


def test_check_tcp_host_returns_failure_on_timeout_error() -> None:
    """check_tcp_host treats TimeoutError as an expected, graceful failure."""

    def fake_connector(addr: tuple[str, int], timeout: float) -> _FakeConn:
        raise TimeoutError("timed out")

    reachable, latency_ms, error = check_tcp_host("192.168.1.1", 22, 3000, fake_connector)
    assert reachable is False
    assert latency_ms is None
    assert error is not None
    assert "timed out" in error


def test_check_tcp_host_propagates_unexpected_exceptions() -> None:
    """Unexpected connector exceptions propagate instead of being swallowed."""

    def fake_connector(addr: tuple[str, int], timeout: float) -> _FakeConn:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        check_tcp_host("192.168.1.1", 22, 3000, fake_connector)


def test_check_tcp_host_invalid_hostname_propagates_validation_error() -> None:
    """An invalid hostname lets ValidationError propagate."""

    def fake_connector(addr: tuple[str, int], timeout: float) -> _FakeConn:
        raise AssertionError("connector must not be called for invalid hostname")

    with pytest.raises(ValidationError):
        check_tcp_host("192.168.1.1;", 22, 3000, fake_connector)


def test_check_tcp_host_timeout_ms_converted_to_seconds() -> None:
    """timeout_ms is converted to seconds for the connector."""

    captured: dict[str, float] = {}

    def fake_connector(addr: tuple[str, int], timeout: float) -> _FakeConn:
        captured["timeout"] = timeout
        return _FakeConn()

    check_tcp_host("192.168.1.1", 22, 500, fake_connector)
    assert captured["timeout"] == 0.5


def test_check_tcp_host_closes_connection_on_success() -> None:
    """The connection object is closed on success."""

    conn = _FakeConn()

    def fake_connector(addr: tuple[str, int], timeout: float) -> _FakeConn:
        return conn

    reachable, _, _ = check_tcp_host("192.168.1.1", 22, 3000, fake_connector)
    assert reachable is True
    assert conn.closed is True


def test_check_tcp_host_no_real_socket_io() -> None:
    """No real socket I/O occurs: the default connector is never invoked."""
    # A fake that would fail loudly if actually given a real socket call.
    captured_host: dict[str, str] = {}

    def fake_connector(addr: tuple[str, int], timeout: float) -> _FakeConn:
        captured_host["addr"] = addr[0]
        return _FakeConn()

    reachable, latency_ms, error = check_tcp_host("example.com", 22, 1000, fake_connector)
    assert reachable is True
    assert latency_ms is not None
    assert error is None
    assert captured_host["addr"] == "example.com"


# ---------------------------------------------------------------------------
# nornir_ssh_check tool tests
# ---------------------------------------------------------------------------


def test_nornir_ssh_check_reachable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable host returns success and structured data."""
    fake_tcp, _ = _tcp_stub((True, 4.2, None))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)

    env = server._nornir_base.nornir_ssh_check(name="spine-01", ctx=_ctx())
    assert env.success is True
    outcome = env.results["spine-01"]
    assert outcome.success is True
    data = outcome.data
    assert data is not None
    assert data["host"] == "spine-01"
    assert data["hostname"] == "192.168.1.1"
    assert data["port"] == 22
    assert data["mode"] == "tcp"
    assert data["reachable"] is True
    assert data["latency_ms"] == 4.2


def test_nornir_ssh_check_data_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Result data includes host, hostname, port, mode, reachable, latency_ms."""
    fake_tcp, _ = _tcp_stub((True, 1.0, None))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)

    env = server._nornir_base.nornir_ssh_check(name="leaf-01", ctx=_ctx())
    data = env.results["leaf-01"].data
    assert data is not None
    assert set(data) == {"host", "hostname", "port", "mode", "reachable", "latency_ms"}
    assert "error_detail" not in data
    assert data["mode"] == "tcp"
    assert data["port"] == 22
    assert data["reachable"] is True


def test_nornir_ssh_check_unreachable_success_when_flag_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreachable host with unreachable_as_error=False succeeds with reachable=False."""
    fake_tcp, _ = _tcp_stub((False, None, "Connection refused"))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)

    env = server._nornir_base.nornir_ssh_check(
        name="spine-01", unreachable_as_error=False, ctx=_ctx()
    )
    assert env.success is True
    outcome = env.results["spine-01"]
    assert outcome.success is True
    data = outcome.data
    assert data is not None
    assert data["reachable"] is False
    assert data["latency_ms"] is None
    assert data["error_detail"] == "Connection refused"


def test_nornir_ssh_check_unreachable_error_when_flag_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreachable host with unreachable_as_error=True fails with connection error."""
    fake_tcp, _ = _tcp_stub((False, None, "Connection refused"))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)

    env = server._nornir_base.nornir_ssh_check(
        name="spine-01", unreachable_as_error=True, ctx=_ctx()
    )
    assert env.success is False
    outcome = env.results["spine-01"]
    assert outcome.success is False
    assert outcome.error is not None
    assert outcome.error.type == "connection"
    assert outcome.error.retryable is True


def test_nornir_ssh_check_invalid_port() -> None:
    """Invalid port returns a request-level validation error."""
    env = server._nornir_base.nornir_ssh_check(name="spine-01", port=0, ctx=_ctx())
    assert env.success is False
    assert env.error is not None
    assert env.error.type == "validation"
    assert "port" in env.error.message


def test_nornir_ssh_check_invalid_timeout() -> None:
    """Invalid timeout_ms returns a request-level validation error."""
    env = server._nornir_base.nornir_ssh_check(name="spine-01", timeout_ms=50, ctx=_ctx())
    assert env.success is False
    assert env.error is not None
    assert env.error.type == "validation"
    assert "timeout_ms" in env.error.message


def test_nornir_ssh_check_no_matching_hosts() -> None:
    """No matching hosts returns a request-level validation error."""
    env = server._nornir_base.nornir_ssh_check(name="nonexistent", ctx=_ctx())
    assert env.success is False
    assert env.error is not None
    assert env.error.type == "validation"


def test_nornir_ssh_check_empty_name_list() -> None:
    """An explicitly empty name list returns a request-level validation error."""
    env = server._nornir_base.nornir_ssh_check(name=[], ctx=_ctx())
    assert env.success is False
    assert env.error is not None
    assert env.error.type == "validation"


def test_nornir_ssh_check_platform_agnostic_junos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Platform-agnostic behavior works for unsupported platforms like junos."""
    hosts_data = {"mx-01": _make_host("mx-01", "192.168.2.1", "junos", ["core"])}
    monkeypatch_inventory(hosts_data, monkeypatch)

    fake_tcp, _ = _tcp_stub((True, 3.3, None))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)
    env = server._nornir_base.nornir_ssh_check(name="mx-01", ctx=_ctx())
    assert env.success is True
    data = env.results["mx-01"].data
    assert data is not None
    assert data["hostname"] == "192.168.2.1"
    assert data["mode"] == "tcp"


def test_nornir_ssh_check_no_netmiko_invocation(
    monkeypatch: pytest.MonkeyPatch, netmiko_fakes: list[dict[str, Any]]
) -> None:
    """No Netmiko fake invocations occur during an ssh check."""
    fake_tcp, _ = _tcp_stub((True, 5.0, None))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)

    server._nornir_base.nornir_ssh_check(ctx=_ctx())
    assert netmiko_fakes == []


def test_nornir_ssh_check_holds_execution_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """nornir_ssh_check runs its selection and probe loop under the execution lock."""
    probe_thread_acquired: list[bool] = []

    def fake_tcp(hostname: str, port: int, timeout_ms: int) -> tuple[bool, float | None, None]:
        # The tool thread already holds EXECUTION_LOCK. RLock is reentrant in
        # the same thread, so probe from a separate thread to prove the lock is
        # held while the probe runs.
        def _try_acquire() -> None:
            acquired = EXECUTION_LOCK.acquire(blocking=False)
            if acquired:
                EXECUTION_LOCK.release()
            probe_thread_acquired.append(acquired)

        thread = threading.Thread(target=_try_acquire)
        thread.start()
        thread.join()
        return (True, 1.0, None)

    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)

    env = server._nornir_base.nornir_ssh_check(name="spine-01", ctx=_ctx())
    assert env.success is True
    assert probe_thread_acquired == [False]


def test_nornir_ssh_check_unexpected_helper_error_isolated_per_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected check_tcp_host exception fails that host, not the request."""

    def fake_tcp(
        hostname: str, port: int, timeout_ms: int
    ) -> tuple[bool, float | None, str | None]:
        raise RuntimeError("boom")

    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)

    env = server._nornir_base.nornir_ssh_check(name="spine-01", ctx=_ctx())
    outcome = env.results["spine-01"]
    assert outcome.success is False
    assert outcome.error is not None
    assert outcome.error.type == "internal"
    assert outcome.error.retryable is False
    assert outcome.error.host == "spine-01"
    assert "ssh check diagnostic failed unexpectedly" in outcome.error.message
    assert "boom" in outcome.error.message


def test_nornir_ssh_check_audit_record_execution_based(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit record is written with execution-based result, no credentials."""
    fake_tcp, _ = _tcp_stub((True, 15.0, None))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)

    env = server._nornir_base.nornir_ssh_check(ctx=_ctx())
    assert env.success is True

    lines = audit.get_audit_logger().log_path.read_text("utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["operation"] == "nornir_ssh_check"
    assert entry["request_id"] == "test-request-id"
    assert set(entry["hosts"]) == {"spine-01", "leaf-01"}
    assert entry["result"] == "success"
    details = entry["details"]
    assert details["port"] == 22
    assert details["mode"] == "tcp"
    assert details["reachable_count"] == 2
    assert details["unreachable_count"] == 0
    assert details["invalid_count"] == 0
    payload = json.dumps(entry)
    assert "password" not in payload
    assert "Connection refused" not in payload


def test_nornir_ssh_check_audit_excludes_error_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit metadata never carries the error_detail of unreachable outcomes."""
    fake_tcp, _ = _tcp_stub((False, None, "TCP connection failed: Connection refused"))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)

    server._nornir_base.nornir_ssh_check(ctx=_ctx())

    lines = audit.get_audit_logger().log_path.read_text("utf-8").strip().splitlines()
    entry = json.loads(lines[0])
    assert "TCP connection failed" not in json.dumps(entry)
    assert entry["details"]["unreachable_count"] == 2


# ---------------------------------------------------------------------------
# Additional coverage gaps
# ---------------------------------------------------------------------------


def test_nornir_ssh_check_list_name_targets_multiple_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list-valued name parameter targets all matching hosts."""
    fake_tcp, _ = _tcp_stub((True, 1.1, None))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)

    env = server._nornir_base.nornir_ssh_check(name=["spine-01", "leaf-01"], ctx=_ctx())
    assert env.success is True
    assert set(env.results) == {"spine-01", "leaf-01"}
    for h in env.results.values():
        assert h.success is True


def test_nornir_ssh_check_invalid_port_65536() -> None:
    """Port 65536 is rejected at the request level."""
    env = server._nornir_base.nornir_ssh_check(name="spine-01", port=65536, ctx=_ctx())
    assert env.success is False
    assert env.error is not None
    assert env.error.type == "validation"


def test_nornir_ssh_check_partial_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """With unreachable_as_error=True, one reachable + one unreachable => 'partial'."""
    results = iter([(True, 5.0, None), (False, None, "Refused")])

    def fake_tcp(
        hostname: str, port: int, timeout_ms: int
    ) -> tuple[bool, float | None, str | None]:
        return next(results)

    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)

    env = server._nornir_base.nornir_ssh_check(unreachable_as_error=True, ctx=_ctx())
    assert env.results["spine-01"].success is True
    assert env.results["leaf-01"].success is False
    assert env.success is False

    lines = audit.get_audit_logger().log_path.read_text("utf-8").strip().splitlines()
    entry = json.loads(lines[0])
    assert entry["result"] == "partial"
    assert entry["details"]["reachable_count"] == 1
    assert entry["details"]["unreachable_count"] == 1


def test_nornir_ssh_check_failed_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """With unreachable_as_error=True, all hosts unreachable => audit 'failed'."""
    fake_tcp, _ = _tcp_stub((False, None, "Refused"))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.check_tcp_host", fake_tcp)

    env = server._nornir_base.nornir_ssh_check(unreachable_as_error=True, ctx=_ctx())
    assert env.success is False
    for outcome in env.results.values():
        assert outcome.success is False

    lines = audit.get_audit_logger().log_path.read_text("utf-8").strip().splitlines()
    entry = json.loads(lines[0])
    assert entry["result"] == "failed"
    assert entry["details"]["reachable_count"] == 0
    assert entry["details"]["unreachable_count"] == 2


def test_nornir_ping_list_name_targets_multiple_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A list-valued name parameter targets all matching hosts."""
    fake_ping, _ = _ping_stub((True, 1.1, None))
    monkeypatch.setattr("nornir_mcp.tools.base.tool.icmp_ping_host", fake_ping)

    env = server._nornir_base.nornir_ping(name=["spine-01", "leaf-01"], ctx=_ctx())
    assert env.success is True
    assert set(env.results) == {"spine-01", "leaf-01"}
    for h in env.results.values():
        assert h.success is True


# ---------------------------------------------------------------------------
# Envelope contract for the connectivity diagnostics
# ---------------------------------------------------------------------------


_CONNECTIVITY_TOOL_CALLS: list[
    tuple[
        str,
        Callable[..., ToolEnvelope],
        Callable[..., tuple[Any, Any]],
        str,
        set[str],
    ]
] = [
    (
        "nornir_ping",
        server._nornir_base.nornir_ping,
        _ping_stub,
        "nornir_mcp.tools.base.tool.icmp_ping_host",
        {"host", "hostname", "mode", "reachable", "latency_ms"},
    ),
    (
        "nornir_ssh_check",
        server._nornir_base.nornir_ssh_check,
        _tcp_stub,
        "nornir_mcp.tools.base.tool.check_tcp_host",
        {"host", "hostname", "port", "mode", "reachable", "latency_ms"},
    ),
]


@pytest.mark.parametrize(
    "tool_name, call_tool, stub_factory, helper_target, data_keys",
    _CONNECTIVITY_TOOL_CALLS,
    ids=[entry[0] for entry in _CONNECTIVITY_TOOL_CALLS],
)
def test_connectivity_tools_speak_envelope_contract(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    call_tool: Callable[..., ToolEnvelope],
    stub_factory: Callable[..., tuple[Any, Any]],
    helper_target: str,
    data_keys: set[str],
) -> None:
    """nornir_ping and nornir_ssh_check speak the §21 ToolEnvelope contract."""
    fake_helper, _ = stub_factory((True, 1.0, None))
    monkeypatch.setattr(helper_target, fake_helper)

    env = call_tool(ctx=_ctx())
    assert isinstance(env, ToolEnvelope)
    assert env.operation == tool_name
    assert isinstance(env.request_id, str) and env.request_id
    assert isinstance(env.results, dict)
    assert env.results  # every inventory host was probed
    for outcome in env.results.values():
        assert isinstance(outcome, HostOutcome)
        assert outcome.success is True
        assert outcome.data is not None
        assert set(outcome.data) == data_keys
