"""Tests for server.py MCP tool definitions.

These tests run against a fake Nornir inventory injected via
the `fake_nornir` fixture — no real devices or SSH sessions involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ValidationError

from nornir_mcp import server
from nornir_mcp.core import audit, runner, storage
from nornir_mcp.core.envelope import HostOutcome, ToolEnvelope
from nornir_mcp.core.errors import BackupError, DeviceConnectionError
from tests.conftest import (
    FakeHost,
    FakeHosts,
    FakeInventory,
    FakeNornir,
    FakeTaskResult,
    fake_netmiko_send_command,
    netmiko_config_transcripts,
)


def _ctx() -> Any:
    """A fake fastmcp Context carrying a stable request_id."""
    return SimpleNamespace(request_id="test-request-id")


@pytest.fixture(autouse=True)
def _reload_server(request: pytest.FixtureRequest) -> Iterator[None]:
    """Reset runner's cached Nornir singleton before each test."""
    # Pulled in for its side effect: patches runner.InitNornir for every test.
    request.getfixturevalue("fake_nornir")
    runner.reset_nornir()
    yield
    runner.reset_nornir()


@pytest.fixture(autouse=True)
def _isolated_backup_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point backup/audit storage at tmp dirs and reset their singletons."""
    monkeypatch.setenv("NORNIR_MCP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("NORNIR_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    storage.reset_backup_store()
    audit.reset_audit_logger()
    yield
    storage.reset_backup_store()
    audit.reset_audit_logger()


# ---------------------------------------------------------------------------
# Tool surface pin
# ---------------------------------------------------------------------------

# Tool names are FROZEN — existing clients (netlab-demo .mcp.json, saved
# workflows) reference them by name. Any rename or addition fails CI here.
FROZEN_TOOL_NAMES = {
    "nornir_list_inventory",
    "nornir_get_facts",
    "nornir_run_getter",
    "nornir_get_config",
    "nornir_list_getters",
    "nornir_reload_inventory",
    "nornir_run_command",
    "nornir_run_commands",
    "nornir_backup_config",
    "nornir_list_backups",
    "nornir_apply_config",
    "nornir_save_config",
}


def test_tool_surface_is_exactly_twelve_frozen_tools() -> None:
    """Pin the exact set of MCP tools exposed by the server.

    Introspects the registered tools on the FastMCP instance itself so the
    test guards the live wire surface, not a hand-maintained list. Extend
    FROZEN_TOOL_NAMES when tools are deliberately added.
    """
    tools = asyncio.run(server.mcp.list_tools())
    assert {tool.name for tool in tools} == FROZEN_TOOL_NAMES


# ---------------------------------------------------------------------------
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
    env = server.nornir_list_inventory(SimpleNamespace())
    assert len(env.request_id) == 32
    assert all(c in "0123456789abcdef" for c in env.request_id)


def test_request_id_falls_back_when_context_raises() -> None:
    """A session-less fastmcp Context raises RuntimeError; fall back to uuid."""

    class _SessionlessCtx:
        @property
        def request_id(self) -> str:
            raise RuntimeError("no MCP session established")

    env = server.nornir_list_inventory(_SessionlessCtx())
    assert len(env.request_id) == 32


# ---------------------------------------------------------------------------
# nornir_list_inventory
# ---------------------------------------------------------------------------


def test_list_inventory_shape() -> None:
    """Verify the envelope and structure of the inventory list."""
    env = server.nornir_list_inventory(_ctx())
    assert env.success is True
    assert env.error is None
    outcome = env.results["server"]
    assert outcome.success is True
    devices = outcome.data
    assert isinstance(devices, list)
    assert {d.name for d in devices} == {"spine-01", "leaf-01"}
    sample = devices[0]
    assert set(sample.model_dump()) == {"name", "hostname", "platform", "groups"}
    assert isinstance(sample.groups, list)


def test_list_inventory_sorted() -> None:
    """Verify that the inventory list is returned sorted by device name."""
    env = server.nornir_list_inventory(_ctx())
    data = env.results["server"].data
    assert data is not None
    names = [d.name for d in data]
    assert names == sorted(names)


def test_list_inventory_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify list_inventory returns empty list when inventory is empty."""

    def mock_init(**_: object) -> FakeNornir:
        return FakeNornir(FakeInventory(FakeHosts({})))

    monkeypatch.setattr("nornir_mcp.core.runner.InitNornir", mock_init)
    runner.reset_nornir()
    env = server.nornir_list_inventory(_ctx())
    assert env.results["server"].data == []
    assert env.success is True


# ---------------------------------------------------------------------------
# nornir_get_facts
# ---------------------------------------------------------------------------


def test_get_facts_returns_envelope() -> None:
    """Verify nornir_get_facts returns per-host facts in a ToolEnvelope."""
    env = server.nornir_get_facts(_ctx(), name="spine-01")
    assert env.success is True
    assert "spine-01" in env.results
    outcome = env.results["spine-01"]
    assert outcome.success is True
    assert outcome.data is not None
    assert outcome.data["facts"]["hostname"] == "test-host"
    assert outcome.data["facts"]["vendor"] == "Arista"


def test_get_facts_by_group() -> None:
    """Verify nornir_get_facts filters by group."""
    env = server.nornir_get_facts(_ctx(), group="spine")
    assert "spine-01" in env.results
    assert "leaf-01" not in env.results


def test_get_facts_by_platform() -> None:
    """Verify nornir_get_facts filters by platform."""
    env = server.nornir_get_facts(_ctx(), platform="eos")
    assert set(env.results.keys()) == {"spine-01", "leaf-01"}


def test_no_matching_hosts_returns_envelope_validation_error_not_raise() -> None:
    """Filter misses become a request-level validation error, not a raise."""
    env = server.nornir_get_facts(_ctx(), name="nonexistent")
    assert env.success is False
    assert env.results == {}
    assert env.error is not None
    assert env.error.type == "validation"
    assert env.error.retryable is False
    assert env.error.operation == "nornir_get_facts"
    assert "No devices match the provided filters" in env.error.message


# ---------------------------------------------------------------------------
# nornir_run_getter
# ---------------------------------------------------------------------------


def test_run_getter_returns_payload() -> None:
    """Verify nornir_run_getter returns the expected payload per host."""
    env = server.nornir_run_getter(_ctx(), getter="arp_table", name="spine-01")
    outcome = env.results["spine-01"]
    assert outcome.success is True
    assert outcome.data is not None
    # napalm_get keys results by the normalized (get_-prefixed) name.
    assert outcome.data["get_arp_table"] == {"ok": True}


def test_run_getter_with_options() -> None:
    """Verify nornir_run_getter passes getter_options through."""
    result = server.nornir_run_getter(
        _ctx(), getter="facts", name="spine-01", getter_options={"keys": ["hostname"]}
    )
    assert result.results["spine-01"] == HostOutcome(
        success=True,
        data={"get_facts": {"hostname": "test-host", "vendor": "Arista", "model": "7280R"}},
    )


def test_run_getter_normalizes_name_and_option_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Getter names/options are get_-prefixed before reaching napalm_get."""
    captured: dict[str, Any] = {}

    def spy(task: Any, operation: str, **kwargs: Any) -> dict[str, HostOutcome]:
        captured["task"] = task
        captured["operation"] = operation
        captured["kwargs"] = kwargs
        return {"spine-01": HostOutcome(success=True)}

    monkeypatch.setattr("nornir_mcp.core.base.run_nornir_task", spy)

    env = server.nornir_run_getter(
        _ctx(), getter="arp_table", name="spine-01", getter_options={"keys": ["x"]}
    )
    assert env.success is True
    assert captured["operation"] == "nornir_run_getter"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["getters"] == ["get_arp_table"]
    assert kwargs["getters_options"] == {"get_arp_table": {"keys": ["x"]}}

    # Already-prefixed names pass through unchanged.
    server.nornir_run_getter(_ctx(), getter="get_facts", name="spine-01")
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["getters"] == ["get_facts"]


def test_run_getter_batch() -> None:
    """Verify nornir_run_getter with multiple devices."""
    result = server.nornir_run_getter(_ctx(), getter="facts", name=["spine-01", "leaf-01"])
    assert set(result.results.keys()) == {"spine-01", "leaf-01"}


# ---------------------------------------------------------------------------
# nornir_get_config
# ---------------------------------------------------------------------------


def test_get_config_returns_config() -> None:
    """Verify nornir_get_config returns config data per host."""
    env = server.nornir_get_config(_ctx(), name="spine-01")
    assert env.success is True
    outcome = env.results["spine-01"]
    assert outcome.success is True
    assert outcome.data is not None
    assert "running" in outcome.data["config"]
    assert "startup" in outcome.data["config"]


def test_get_config_running_only() -> None:
    """Verify nornir_get_config with retrieve='running'."""
    env = server.nornir_get_config(_ctx(), name="spine-01", retrieve="running")
    data = env.results["spine-01"].data
    assert data is not None
    assert data["config"]["running"] is not None


def test_get_config_sanitized_defaults_true() -> None:
    """Sanitized output is the default (never expose credentials, §22)."""
    params = inspect.signature(server.nornir_get_config).parameters
    assert params["sanitized"].default is True
    doc = server.nornir_get_config.__doc__ or ""
    assert "password hashes" in doc
    assert "pre-shared keys" in doc
    assert "strips" in doc


def test_get_config_retrieve_rejects_invalid_literal() -> None:
    """retrieve is a literal constrained to running/startup/all."""
    tool = asyncio.run(server.mcp.get_tool("nornir_get_config"))
    assert tool is not None
    retrieve = tool.parameters["properties"]["retrieve"]
    assert retrieve["enum"] == ["running", "startup", "all"]
    assert retrieve["default"] == "all"

    with pytest.raises(ValidationError):
        asyncio.run(server.mcp.call_tool("nornir_get_config", {"retrieve": "bogus"}))


def test_config_format_parameter_accepted() -> None:
    """The format knob is config_format (no builtin shadowing) and works."""
    params = inspect.signature(server.nornir_get_config).parameters
    assert "config_format" in params
    assert "format" not in params
    env = server.nornir_get_config(_ctx(), name="spine-01", config_format="json")
    assert env.success is True
    data = env.results["spine-01"].data
    assert data is not None
    assert "running" in data["config"]


def test_empty_name_list_returns_validation_envelope() -> None:
    """An explicit empty selection surfaces as a validation envelope error."""
    env = server.nornir_get_facts(_ctx(), name=[])
    assert env.success is False
    assert env.results == {}
    assert env.error is not None
    assert env.error.type == "validation"
    assert env.error.message == "explicitly empty device list provided"


_NO_FILTER_TOOLS: list[tuple[str, Callable[[Any], ToolEnvelope]]] = [
    ("nornir_get_facts", lambda ctx: server.nornir_get_facts(ctx)),
    ("nornir_run_getter", lambda ctx: server.nornir_run_getter(ctx, getter="facts")),
    ("nornir_get_config", lambda ctx: server.nornir_get_config(ctx)),
]


@pytest.mark.parametrize("name, call_tool", _NO_FILTER_TOOLS, ids=[c[0] for c in _NO_FILTER_TOOLS])
def test_no_filters_targets_all_devices(
    name: str, call_tool: Callable[[Any], ToolEnvelope]
) -> None:
    """Omitted filters target every device, and docstrings say so explicitly."""
    env = call_tool(_ctx())
    assert set(env.results.keys()) == {"spine-01", "leaf-01"}
    doc = getattr(server, name).__doc__ or ""
    assert "Omit all filters to target every device in the inventory" in doc


# ---------------------------------------------------------------------------
# nornir_list_getters
# ---------------------------------------------------------------------------


def test_list_getters_returns_platforms() -> None:
    """Verify list_getters returns GetterInfo for each platform."""
    env = server.nornir_list_getters(_ctx())
    assert env.success is True
    data = env.results["server"].data
    assert data is not None
    platforms = {r.platform for r in data}
    assert "eos" in platforms


def test_list_getters_has_getters() -> None:
    """Verify the getter lists are non-empty for known platforms."""
    data = server.nornir_list_getters(_ctx()).results["server"].data
    assert data is not None
    for info in data:
        if info.platform == "eos":
            assert len(info.getters) > 0
            assert "facts" in info.getters


def test_list_getters_sorted_by_platform() -> None:
    """Verify results are sorted by platform name."""
    data = server.nornir_list_getters(_ctx()).results["server"].data
    assert data is not None
    names = [r.platform for r in data]
    assert names == sorted(names)


def test_list_getters_unknown_platform_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify list_getters returns empty getters for unknown platform."""
    hosts_data = {
        "bogus": FakeHost(name="bogus", hostname="10.0.0.1", platform="nonexistent_os", groups=[]),
    }

    def mock_init(**_: object) -> FakeNornir:
        return FakeNornir(FakeInventory(FakeHosts(hosts_data)))

    monkeypatch.setattr("nornir_mcp.core.runner.InitNornir", mock_init)
    runner.reset_nornir()
    data = server.nornir_list_getters(_ctx()).results["server"].data
    assert data is not None
    assert len(data) == 1
    assert data[0].platform == "nonexistent_os"
    assert data[0].getters == []
    assert data[0].error is not None  # failure surfaced in the error field


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
    env = server.nornir_run_command(command="show version", name="spine-01", ctx=_ctx())
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
    env = server.nornir_run_command(command="reload", name="spine-01", ctx=_ctx())
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
    env = server.nornir_run_command(command="show version\nreload", ctx=_ctx())
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
    env = server.nornir_run_command(command="show version", name="mx-01", ctx=_ctx())
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
    env = server.nornir_run_command(command="show version", name="spine-01", ctx=_ctx())
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
    env = server.nornir_run_command(command="show version", ctx=_ctx())
    # Both outcomes preserved; only the passing host executed.
    assert env.success is False  # mx-01 failed its gate
    assert env.results["spine-01"].success is True
    assert env.results["mx-01"].success is False
    assert env.results["mx-01"].error is not None
    assert env.results["mx-01"].error.type == "unsupported_operation"
    assert [call["host"] for call in netmiko_fakes] == ["spine-01"]


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


# ---------------------------------------------------------------------------
# nornir_apply_config (spec §8, §16)
# ---------------------------------------------------------------------------


def test_apply_dry_run_returns_planned_commands_only_and_touches_nothing(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """dry_run=True returns §16.1-labeled outcomes with ZERO device calls."""
    env = server.nornir_apply_config(["interface Ethernet1", "description uplink"], ctx=_ctx())
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
    sig = inspect.signature(server.nornir_apply_config)
    assert sig.parameters["dry_run"].default is True
    assert "backup" not in sig.parameters


def test_apply_dry_run_writes_audit_record() -> None:
    """Dry runs are audited with result 'dry_run' and a hash, never config text."""
    server.nornir_apply_config(["interface Ethernet1"], ctx=_ctx())
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

    monkeypatch.setattr(server, "capture_pre_change_backups", failing_backups)
    env = server.nornir_apply_config(["interface Ethernet1"], dry_run=False, ctx=_ctx())
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
    env = server.nornir_apply_config(config, dry_run=False, ctx=_ctx())
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
    env = server.nornir_apply_config(["interface Ethernet1"], dry_run=False, ctx=_ctx())
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
    env = server.nornir_apply_config(
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
    env = server.nornir_apply_config(config, dry_run=False, ctx=_ctx())
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
    env = server.nornir_apply_config(["interface Ethernet1"], dry_run=False, ctx=_ctx())
    assert env.error is None
    assert env.success is False
    assert env.results["spine-01"].success is False
    assert env.results["leaf-01"].success is True


# ---------------------------------------------------------------------------
# nornir_save_config (spec §11)
# ---------------------------------------------------------------------------


def test_save_config_success_envelope(netmiko_fakes: list[dict[str, Any]]) -> None:
    """Both capable hosts save and report success."""
    env = server.nornir_save_config(ctx=_ctx())
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
    env = server.nornir_save_config(ctx=_ctx())
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
    env = server.nornir_save_config(ctx=_ctx())
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

    monkeypatch.setattr(server, "netmiko_save_config", fake_netmiko_save_config_flaky)
    env = server.nornir_save_config(ctx=_ctx())
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
    env = server.nornir_apply_config(["interface Ethernet1"], dry_run=False, ctx=_ctx())
    assert env.success is True
    functions = [c["function"] for c in netmiko_fakes]
    assert "netmiko_save_config" not in functions
    assert "netmiko_send_config" in functions
