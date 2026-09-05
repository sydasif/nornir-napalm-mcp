"""Tests for NAPALM tools — get_facts, run_getter, get_config, list_getters."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ValidationError

from nornir_mcp import server
from nornir_mcp.core import audit, runner, storage
from nornir_mcp.core.envelope import HostOutcome, ToolEnvelope
from tests.conftest import (
    FakeHost,
    FakeHosts,
    FakeInventory,
    FakeNornir,
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
# nornir_list_inventory
# ---------------------------------------------------------------------------


def test_list_inventory_shape() -> None:
    """Verify the envelope and structure of the inventory list."""
    env = server._nornir_base.nornir_list_inventory(_ctx())
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
    env = server._nornir_base.nornir_list_inventory(_ctx())
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
    env = server._nornir_base.nornir_list_inventory(_ctx())
    assert env.results["server"].data == []
    assert env.success is True


# ---------------------------------------------------------------------------
# nornir_get_facts
# ---------------------------------------------------------------------------


def test_get_facts_returns_envelope() -> None:
    """Verify nornir_get_facts returns per-host facts in a ToolEnvelope."""
    env = server._napalm_tools.nornir_get_facts(_ctx(), name="spine-01")
    assert env.success is True
    assert "spine-01" in env.results
    outcome = env.results["spine-01"]
    assert outcome.success is True
    assert outcome.data is not None
    assert outcome.data["facts"]["hostname"] == "test-host"
    assert outcome.data["facts"]["vendor"] == "Arista"


def test_get_facts_by_group() -> None:
    """Verify nornir_get_facts filters by group."""
    env = server._napalm_tools.nornir_get_facts(_ctx(), group="spine")
    assert "spine-01" in env.results
    assert "leaf-01" not in env.results


def test_get_facts_by_platform() -> None:
    """Verify nornir_get_facts filters by platform."""
    env = server._napalm_tools.nornir_get_facts(_ctx(), platform="eos")
    assert set(env.results.keys()) == {"spine-01", "leaf-01"}


def test_no_matching_hosts_returns_envelope_validation_error_not_raise() -> None:
    """Filter misses become a request-level validation error, not a raise."""
    env = server._napalm_tools.nornir_get_facts(_ctx(), name="nonexistent")
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
    env = server._napalm_tools.nornir_run_getter(_ctx(), getter="arp_table", name="spine-01")
    outcome = env.results["spine-01"]
    assert outcome.success is True
    assert outcome.data is not None
    # napalm_get keys results by the normalized (get_-prefixed) name.
    assert outcome.data["get_arp_table"] == {"ok": True}


def test_run_getter_with_options() -> None:
    """Verify nornir_run_getter passes getter_options through."""
    result = server._napalm_tools.nornir_run_getter(
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

    env = server._napalm_tools.nornir_run_getter(
        _ctx(), getter="arp_table", name="spine-01", getter_options={"keys": ["x"]}
    )
    assert env.success is True
    assert captured["operation"] == "nornir_run_getter"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["getters"] == ["get_arp_table"]
    assert kwargs["getters_options"] == {"get_arp_table": {"keys": ["x"]}}

    # Already-prefixed names pass through unchanged.
    server._napalm_tools.nornir_run_getter(_ctx(), getter="get_facts", name="spine-01")
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["getters"] == ["get_facts"]


def test_run_getter_batch() -> None:
    """Verify nornir_run_getter with multiple devices."""
    result = server._napalm_tools.nornir_run_getter(
        _ctx(), getter="facts", name=["spine-01", "leaf-01"]
    )
    assert set(result.results.keys()) == {"spine-01", "leaf-01"}


# ---------------------------------------------------------------------------
# nornir_get_config
# ---------------------------------------------------------------------------


def test_get_config_returns_config() -> None:
    """Verify nornir_get_config returns config data per host."""
    env = server._napalm_tools.nornir_get_config(_ctx(), name="spine-01")
    assert env.success is True
    outcome = env.results["spine-01"]
    assert outcome.success is True
    assert outcome.data is not None
    assert "running" in outcome.data["config"]
    assert "startup" in outcome.data["config"]


def test_get_config_running_only() -> None:
    """Verify nornir_get_config with retrieve='running'."""
    env = server._napalm_tools.nornir_get_config(_ctx(), name="spine-01", retrieve="running")
    data = env.results["spine-01"].data
    assert data is not None
    assert data["config"]["running"] is not None


def test_get_config_sanitized_defaults_true() -> None:
    """Sanitized output is the default (never expose credentials, §22)."""
    params = inspect.signature(server._napalm_tools.nornir_get_config).parameters
    assert params["sanitized"].default is True
    doc = server._napalm_tools.nornir_get_config.__doc__ or ""
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
    params = inspect.signature(server._napalm_tools.nornir_get_config).parameters
    assert "config_format" in params
    assert "format" not in params
    env = server._napalm_tools.nornir_get_config(_ctx(), name="spine-01", config_format="json")
    assert env.success is True
    data = env.results["spine-01"].data
    assert data is not None
    assert "running" in data["config"]


def test_empty_name_list_returns_validation_envelope() -> None:
    """An explicit empty selection surfaces as a validation envelope error."""
    env = server._napalm_tools.nornir_get_facts(_ctx(), name=[])
    assert env.success is False
    assert env.results == {}
    assert env.error is not None
    assert env.error.type == "validation"
    assert env.error.message == "explicitly empty device list provided"


_NO_FILTER_TOOLS: list[tuple[str, Callable[[Any], ToolEnvelope]]] = [
    ("nornir_get_facts", lambda ctx: server._napalm_tools.nornir_get_facts(ctx)),
    ("nornir_run_getter", lambda ctx: server._napalm_tools.nornir_run_getter(ctx, getter="facts")),
    ("nornir_get_config", lambda ctx: server._napalm_tools.nornir_get_config(ctx)),
]


@pytest.mark.parametrize("name, call_tool", _NO_FILTER_TOOLS, ids=[c[0] for c in _NO_FILTER_TOOLS])
def test_no_filters_targets_all_devices(
    name: str, call_tool: Callable[[Any], ToolEnvelope]
) -> None:
    """Omitted filters target every device, and docstrings say so explicitly."""
    env = call_tool(_ctx())
    assert set(env.results.keys()) == {"spine-01", "leaf-01"}
    doc = getattr(server._napalm_tools, name).__doc__ or ""
    assert "Omit all filters to target every device in the inventory" in doc


# ---------------------------------------------------------------------------
# nornir_list_getters
# ---------------------------------------------------------------------------


def test_list_getters_returns_platforms() -> None:
    """Verify list_getters returns GetterInfo for each platform."""
    env = server._napalm_tools.nornir_list_getters(_ctx())
    assert env.success is True
    data = env.results["server"].data
    assert data is not None
    platforms = {r.platform for r in data}
    assert "eos" in platforms


def test_list_getters_has_getters() -> None:
    """Verify the getter lists are non-empty for known platforms."""
    data = server._napalm_tools.nornir_list_getters(_ctx()).results["server"].data
    assert data is not None
    for info in data:
        if info.platform == "eos":
            assert len(info.getters) > 0
            assert "facts" in info.getters


def test_list_getters_sorted_by_platform() -> None:
    """Verify results are sorted by platform name."""
    data = server._napalm_tools.nornir_list_getters(_ctx()).results["server"].data
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
    data = server._napalm_tools.nornir_list_getters(_ctx()).results["server"].data
    assert data is not None
    assert len(data) == 1
    assert data[0].platform == "nonexistent_os"
    assert data[0].getters == []
    assert data[0].error is not None  # failure surfaced in the error field
