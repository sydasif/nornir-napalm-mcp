"""Tests for server.py MCP tool definitions.

These tests run against a fake Nornir inventory injected via
the `fake_nornir` fixture — no real devices or SSH sessions involved.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nornir_mcp import server
from nornir_mcp.core import audit, runner, storage
from nornir_mcp.core.envelope import HostOutcome, ToolEnvelope
from nornir_mcp.core.errors import DeviceConnectionError
from tests.conftest import (
    FakeHost,
    FakeHosts,
    FakeInventory,
    FakeNornir,
    fake_netmiko_send_command,
)


def _ctx() -> Any:
    """A fake fastmcp Context carrying a stable request_id."""
    return SimpleNamespace(request_id="test-request-id")


@pytest.fixture(autouse=True)
def _reload_server(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Reset runner's cached Nornir singleton before each test."""
    # Pulled in for its side effect: patches runner.InitNornir for every test.
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


# Tool envelope contract
# ---------------------------------------------------------------------------

_TOOL_CALLS: list[tuple[str, Callable[[Any], ToolEnvelope]]] = [
    ("nornir_list_inventory", lambda ctx: server.nornir_list_inventory(ctx)),
    ("nornir_get_facts", lambda ctx: server.nornir_get_facts(ctx)),
    ("nornir_run_getter", lambda ctx: server.nornir_run_getter(ctx, getter="facts")),
    ("nornir_get_config", lambda ctx: server.nornir_get_config(ctx)),
    ("nornir_list_getters", lambda ctx: server.nornir_list_getters(ctx)),
]


@pytest.mark.parametrize("name, call_tool", _TOOL_CALLS, ids=[c[0] for c in _TOOL_CALLS])
def test_every_migrated_tool_returns_envelope(
    name: str, call_tool: Callable[[Any], ToolEnvelope]
) -> None:
    """Every result-returning tool speaks the §21 ToolEnvelope contract."""
    env = call_tool(_ctx())
    assert isinstance(env, ToolEnvelope)
    assert env.operation == name
    assert isinstance(env.request_id, str) and env.request_id
    assert isinstance(env.results, dict)
    assert all(isinstance(outcome, HostOutcome) for outcome in env.results.values())


def test_request_id_comes_from_context_when_available() -> None:
    """The envelope request_id is taken from the injected Context."""
    env = server.nornir_list_inventory(_ctx())
    assert env.request_id == "test-request-id"


def test_request_id_falls_back_to_uuid_when_context_has_none() -> None:
    """A ctx without request_id yields a uuid4().hex fallback."""
    env = server.nornir_list_inventory(SimpleNamespace(request_id=None))  # type: ignore[arg-type]
    assert len(env.request_id) == 32
    assert all(c in "0123456789abcdef" for c in env.request_id)


def test_request_id_falls_back_when_context_raises() -> None:
    """A session-less fastmcp Context raises RuntimeError; fall back to uuid."""

    class _SessionlessCtx:
        @property
        def request_id(self) -> str:
            raise RuntimeError("no MCP session established")

    env = server.nornir_list_inventory(_SessionlessCtx())  # type: ignore[arg-type]
    assert len(env.request_id) == 32


# ---------------------------------------------------------------------------
# nornir_reload_inventory
# ---------------------------------------------------------------------------


def test_reload_inventory() -> None:
    """Verify reload returns a success envelope and clears the cache."""
    runner.get_nornir()
    env = server.nornir_reload_inventory(_ctx())
    assert env.success is True
    assert env.results == {}
    assert env.error is None
    # After reload, calling get_nornir() should create a new instance
    nr = runner.get_nornir()
    assert nr is not None


# ---------------------------------------------------------------------------
# nornir_run_commands
# ---------------------------------------------------------------------------


def test_batch_all_allowed_returns_per_command_map(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """An all-allowed batch returns one truncated output per command."""
    env = server.nornir_run_commands(
        commands=["show version", "show ip route"], name="spine-01", ctx=_ctx()
    )
    assert env.success is True
    data = env.results["spine-01"].data
    assert data is not None
    assert data["rejected"] == {}
    commands = data["commands"]
    assert set(commands) == {"show version", "show ip route"}
    out = commands["show version"]
    assert out["output"] == "canned output [spine-01]: show version"
    assert out["truncated"] is False
    assert out["original_size"] == len("canned output [spine-01]: show version")
    # Exactly the allowed set was sent, once.
    assert len(netmiko_fakes) == 1
    assert netmiko_fakes[0]["function"] == "netmiko_send_commands"
    assert netmiko_fakes[0]["commands"] == ["show version", "show ip route"]


def test_batch_one_dangerous_command_rejected_others_executed(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """A disallowed command fails only itself; the rest still run (§7.2)."""
    env = server.nornir_run_commands(
        commands=["show version", "reload", "show interfaces"], name="spine-01", ctx=_ctx()
    )
    assert env.success is True  # some commands ran
    data = env.results["spine-01"].data
    assert data is not None
    assert set(data["commands"]) == {"show version", "show interfaces"}
    rejected = data["rejected"]
    assert set(rejected) == {"reload"}
    assert "dangerous" in rejected["reload"]["error"]
    # The rejected command was never sent.
    assert len(netmiko_fakes) == 1
    assert netmiko_fakes[0]["commands"] == ["show version", "show interfaces"]


def test_batch_all_rejected_executes_nothing(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """When every command is rejected the host fails and nothing is sent."""
    env = server.nornir_run_commands(commands=["reload", "wr e"], name="spine-01", ctx=_ctx())
    assert env.success is False
    outcome = env.results["spine-01"]
    assert outcome.success is False
    assert outcome.error is not None
    assert outcome.error.type == "command_rejected"
    data = outcome.data
    assert data is not None
    assert data["commands"] == {}
    assert set(data["rejected"]) == {"reload", "wr e"}
    assert netmiko_fakes == []


def test_batch_order_preserved_in_results(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """Command results keep the original batch order."""
    commands = ["show version", "show arp", "show ip route"]
    env = server.nornir_run_commands(commands=commands, name="spine-01", ctx=_ctx())
    data = env.results["spine-01"].data
    assert data is not None
    assert list(data["commands"]) == commands
    assert netmiko_fakes[0]["commands"] == commands


def test_batch_empty_list_rejected() -> None:
    """An empty commands list is a request-level validation error."""
    env = server.nornir_run_commands(commands=[], name="spine-01", ctx=_ctx())
    assert env.success is False
    assert env.results == {}
    assert env.error is not None
    assert env.error.type == "validation"


def test_batch_newline_in_any_command_rejects_request(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """Any malformed command fails the whole batch; nothing is sent."""
    env = server.nornir_run_commands(
        commands=["show version", "show version\nreload"], name="spine-01", ctx=_ctx()
    )
    assert env.success is False
    assert env.results == {}
    assert env.error is not None
    assert env.error.type == "validation"
    assert netmiko_fakes == []


# ---------------------------------------------------------------------------
# nornir_backup_config / nornir_list_backups
# ---------------------------------------------------------------------------


def test_backup_config_stores_file_and_returns_record() -> None:
    """A backup writes an immutable .cfg and returns its metadata."""
    env = server.nornir_backup_config(name="spine-01", ctx=_ctx())
    assert env.success is True
    data = env.results["spine-01"].data
    assert data is not None
    assert set(data) == {"backup_id", "path", "sha256", "size", "timestamp"}

    # The captured running config was stored verbatim — backups are raw by
    # design (spec §8 rollback substrate), secrets included.
    running_config = "! running-config\nhostname test-host\nenable secret 5 $1$e2e$f00\n"
    assert data["sha256"] == hashlib.sha256(running_config.encode()).hexdigest()
    assert Path(data["path"]).read_text("utf-8") == running_config

    records = storage.get_backup_store().list("spine-01")
    assert len(records) == 1
    assert records[0].backup_id == data["backup_id"]
    assert records[0].trigger == "standalone"


def test_backup_config_napalm_unavailable_falls_back_to_netmiko(
    monkeypatch: pytest.MonkeyPatch, netmiko_fakes: list[dict[str, Any]]
) -> None:
    """Platforms without a NAPALM driver fall back to show running-config."""
    monkeypatch.setattr(
        "nornir_mcp.tools.base.capture.napalm.get_network_driver",
        lambda _p: (_ for _ in ()).throw(ValueError("no driver")),
    )
    monkeypatch.setattr(
        "nornir_mcp.tools.base.capture.netmiko_send_command", fake_netmiko_send_command
    )

    env = server.nornir_backup_config(name="spine-01", ctx=_ctx())
    assert env.success is True
    data = env.results["spine-01"].data
    assert data is not None
    canned = "canned output [spine-01]: show running-config"
    assert Path(data["path"]).read_text("utf-8") == canned
    assert netmiko_fakes[-1]["command_string"] == "show running-config"


def test_backup_config_partial_failure_preserves_successes(
    monkeypatch: pytest.MonkeyPatch, netmiko_fakes: list[dict[str, Any]]
) -> None:
    """One host's capture failure does not stop the others (§21)."""
    hosts = {
        "spine-01": FakeHost(name="spine-01", hostname="10.0.0.1", platform="eos", groups=[]),
        "mx-01": FakeHost(name="mx-01", hostname="10.0.0.9", platform="junos", groups=[]),
    }

    def mock_init(**_: object) -> FakeNornir:
        return FakeNornir(FakeInventory(FakeHosts(hosts)))

    monkeypatch.setattr("nornir_mcp.core.runner.InitNornir", mock_init)
    runner.reset_nornir()

    # Force the netmiko fallback and make it fail only for mx-01.
    monkeypatch.setattr(
        "nornir_mcp.tools.base.capture.napalm.get_network_driver",
        lambda _p: (_ for _ in ()).throw(ValueError("no driver")),
    )

    def fake_netmiko_send_command_failing(
        task: Any, command_string: str = "", **kw: Any
    ) -> object:
        if task.host.name == "mx-01":
            raise DeviceConnectionError("connect failed", host="mx-01")
        return fake_netmiko_send_command(task, command_string=command_string, **kw)

    monkeypatch.setattr(
        "nornir_mcp.tools.base.capture.netmiko_send_command", fake_netmiko_send_command_failing
    )

    env = server.nornir_backup_config(ctx=_ctx())
    assert env.success is False
    assert env.results["spine-01"].success is True
    assert env.results["mx-01"].success is False
    assert env.results["mx-01"].error is not None
    assert env.results["mx-01"].error.type == "connection"
    assert storage.get_backup_store().list("spine-01")
    assert storage.get_backup_store().list("mx-01") == []


def test_backup_config_writes_audit_line() -> None:
    """A backup appends one audit line with hashes only (spec §25)."""
    env = server.nornir_backup_config(name="spine-01", ctx=_ctx())
    assert env.success is True

    lines = audit.get_audit_logger().log_path.read_text("utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["operation"] == "nornir_backup_config"
    assert entry["request_id"] == "test-request-id"
    assert entry["hosts"] == ["spine-01"]
    assert entry["result"] == "success"
    shas = entry["details"]["sha256"]
    assert set(shas) == {"spine-01"}
    assert len(shas["spine-01"]) == 64  # sha256 hex


def test_list_backups_returns_sorted_records() -> None:
    """list_backups returns stored records, oldest first."""
    store = storage.get_backup_store()
    store.save("spine-01", "config one", trigger="standalone")
    store.save("spine-01", "config two", trigger="standalone")

    env = server.nornir_list_backups(host="spine-01", ctx=_ctx())
    assert env.success is True
    data = env.results["server"].data
    assert data is not None
    assert len(data) == 2
    assert [r.backup_id for r in data] == sorted(r.backup_id for r in data)
    assert data[0].size == len(b"config one")


def test_list_backups_rejects_traversal_host() -> None:
    """Unsafe host names fail at request level, before filesystem access."""
    env = server.nornir_list_backups(host="../etc", ctx=_ctx())
    assert env.success is False
    assert env.results == {}
    assert env.error is not None
    assert env.error.type == "validation"
    assert "identifier" in env.error.message
