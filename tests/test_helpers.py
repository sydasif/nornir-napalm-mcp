"""Tests for server.py helpers and tool entry points.

These tests run against a fake Nornir inventory injected via
the `fake_nornir` fixture — no real devices or SSH sessions involved.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from nornir_napalm_mcp import runner, server
from tests.conftest import FakeGroup, FakeHost, FakeHosts, FakeInventory, FakeNornir


@pytest.fixture(autouse=True)
def _reload_server(fake_nornir: dict[str, FakeHost]) -> Iterator[None]:
    """Reset runner's cached Nornir singleton before each test."""
    runner._get_nornir.cache_clear()
    yield
    runner._get_nornir.cache_clear()


def test_list_inventory_shape() -> None:
    """Verify the structure and content of the inventory list."""
    devices = server.nornir_list_inventory()
    assert isinstance(devices, list)
    assert {d.name for d in devices} == {"spine-01", "leaf-01"}
    sample = devices[0]
    assert set(sample.model_dump()) == {"name", "hostname", "platform", "groups"}
    assert isinstance(sample.groups, list)


def test_list_inventory_sorted() -> None:
    """Verify that the inventory list is returned sorted by device name."""
    devices = server.nornir_list_inventory()
    names = [d.name for d in devices]
    assert names == sorted(names)


def test_get_facts_returns_dict() -> None:
    """Verify nornir_get_facts returns raw dict wrapped in getter key."""
    facts = server.nornir_get_facts(name="spine-01")
    assert isinstance(facts, dict)
    assert "spine-01" in facts
    assert facts["spine-01"]["facts"]["hostname"] == "test-host"
    assert facts["spine-01"]["facts"]["vendor"] == "Arista"


def test_get_facts_by_group() -> None:
    """Verify nornir_get_facts filters by group."""
    facts = server.nornir_get_facts(group="spine")
    assert "spine-01" in facts
    assert "leaf-01" not in facts


def test_get_facts_by_platform() -> None:
    """Verify nornir_get_facts filters by platform."""
    facts = server.nornir_get_facts(platform="eos")
    assert set(facts.keys()) == {"spine-01", "leaf-01"}


def test_get_facts_no_match_raises() -> None:
    """Verify nornir_get_facts raises ValueError when no devices match."""
    with pytest.raises(ValueError, match="No devices match filters"):
        server.nornir_get_facts(name="nonexistent")


def test_run_getter_returns_payload() -> None:
    """Verify nornir_run_getter returns the expected payload wrapped in getter key."""
    out = server.nornir_run_getter(getter="arp_table", name="spine-01")
    assert isinstance(out, dict)
    assert "spine-01" in out
    assert out["spine-01"]["arp_table"] == {"ok": True}


def test_run_getter_with_options() -> None:
    """Verify nornir_run_getter passes getter_options through."""
    result = server.nornir_run_getter(
        getter="facts", name="spine-01", getter_options={"keys": ["hostname"]}
    )
    assert result == {
        "spine-01": {"facts": {"hostname": "test-host", "vendor": "Arista", "model": "7280R"}}
    }


def test_run_getter_batch() -> None:
    """Verify nornir_run_getter with multiple devices."""
    result = server.nornir_run_getter(getter="facts", name=["spine-01", "leaf-01"])
    assert isinstance(result, dict)
    assert set(result.keys()) == {"spine-01", "leaf-01"}


def test_get_config_returns_config() -> None:
    """Verify nornir_get_config returns config data wrapped in getter key."""
    cfg = server.nornir_get_config(name="spine-01")
    assert isinstance(cfg, dict)
    assert "spine-01" in cfg
    assert "running" in cfg["spine-01"]["config"]
    assert "startup" in cfg["spine-01"]["config"]


def test_get_config_running_only() -> None:
    """Verify nornir_get_config with retrieve='running'."""
    cfg = server.nornir_get_config(name="spine-01", retrieve="running")
    assert cfg["spine-01"]["config"]["running"] is not None


def test_run_cli_returns_output() -> None:
    """Verify nornir_run_cli returns command output."""
    out = server.nornir_run_cli(commands=["show version"], name="spine-01")
    assert isinstance(out, dict)
    assert "spine-01" in out
    assert "show version" in out["spine-01"]


def test_run_cli_multiple_commands() -> None:
    """Verify nornir_run_cli handles multiple commands."""
    out = server.nornir_run_cli(commands=["show version", "show ip route"], name="spine-01")
    assert "show version" in out["spine-01"]
    assert "show ip route" in out["spine-01"]


def test_run_cli_batch() -> None:
    """Verify nornir_run_cli with multiple devices."""
    result = server.nornir_run_cli(commands=["show version"], name=["spine-01", "leaf-01"])
    assert isinstance(result, dict)
    assert set(result.keys()) == {"spine-01", "leaf-01"}


def test_nornir_ping_returns_result() -> None:
    """Verify nornir_ping returns ping results."""
    result = server.nornir_ping(destination="10.0.0.1", name="spine-01")
    assert isinstance(result, dict)
    assert "spine-01" in result
    assert "rtt_avg" in result["spine-01"]
    assert result["spine-01"]["packet_loss"] == 0


def test_nornir_ping_with_options() -> None:
    """Verify nornir_ping accepts all optional parameters."""
    result = server.nornir_ping(
        destination="10.0.0.1",
        name="spine-01",
        source="10.0.0.2",
        ttl=64,
        timeout=5,
        size=200,
        count=10,
        vrf="mgmt",
    )
    assert "spine-01" in result
    assert result["spine-01"]["results_sent"] == 10


def test_nornir_ping_by_group() -> None:
    """Verify nornir_ping filters by group."""
    result = server.nornir_ping(destination="10.0.0.1", group="leaf")
    assert "leaf-01" in result
    assert "spine-01" not in result


def test_list_getters_returns_platforms() -> None:
    """Verify list_getters returns GetterInfo for each platform."""
    results = server.nornir_list_getters()
    assert isinstance(results, list)
    platforms = {r.platform for r in results}
    assert "eos" in platforms


def test_list_getters_has_getters() -> None:
    """Verify the getter lists are non-empty for known platforms."""
    results = server.nornir_list_getters()
    for info in results:
        if info.platform == "eos":
            assert len(info.getters) > 0
            assert "facts" in info.getters


def test_list_getters_sorted_by_platform() -> None:
    """Verify results are sorted by platform name."""
    results = server.nornir_list_getters()
    names = [r.platform for r in results]
    assert names == sorted(names)


def test_list_getters_unknown_platform_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify list_getters returns empty getters for unknown platform."""
    hosts_data = {
        "bogus": FakeHost(name="bogus", hostname="10.0.0.1", platform="nonexistent_os", groups=[]),
    }

    def mock_init(**_: object) -> FakeNornir:
        return FakeNornir(FakeInventory(FakeHosts(hosts_data)))

    monkeypatch.setattr("nornir_napalm_mcp.runner.InitNornir", mock_init)
    runner._get_nornir.cache_clear()
    results = server.nornir_list_getters()
    assert len(results) == 1
    assert results[0].platform == "nonexistent_os"
    assert results[0].getters == []


def test_reload_inventory() -> None:
    """Verify inventory reload clears the cache."""
    runner._get_nornir()
    server.nornir_reload_inventory()
    # After reload, calling _get_nornir should create a new instance
    nr = runner._get_nornir()
    assert nr is not None


def test_filter_devices_empty_raises() -> None:
    """Verify _filter_devices raises ValueError when no devices match."""
    nr = FakeNornir(FakeInventory(FakeHosts({})))
    with pytest.raises(ValueError, match="No devices match filters"):
        server._filter_devices(nr, name="nonexistent")  # type: ignore[arg-type]


def test_filter_devices_by_name_list() -> None:
    """Verify _filter_devices filters by list of names."""
    hosts = {
        "a": FakeHost(name="a", hostname="10.0.0.1", platform="eos", groups=[]),
        "b": FakeHost(name="b", hostname="10.0.0.2", platform="eos", groups=[]),
    }
    nr = FakeNornir(FakeInventory(FakeHosts(hosts)))
    filtered = server._filter_devices(nr, name=["a"])  # type: ignore[arg-type]
    assert set(filtered.inventory.hosts._hosts.keys()) == {"a"}  # type: ignore[attr-defined]


def test_filter_devices_by_group() -> None:
    """Verify _filter_devices filters by group."""
    hosts = {
        "r1": FakeHost(
            name="r1",
            hostname="10.0.0.1",
            platform="eos",
            groups=[FakeGroup(name="core")],
        ),
        "r2": FakeHost(
            name="r2",
            hostname="10.0.0.2",
            platform="eos",
            groups=[FakeGroup(name="edge")],
        ),
    }
    nr = FakeNornir(FakeInventory(FakeHosts(hosts)))
    filtered = server._filter_devices(nr, group="core")  # type: ignore[arg-type]
    assert set(filtered.inventory.hosts._hosts.keys()) == {"r1"}  # type: ignore[attr-defined]


def test_filter_devices_by_platform() -> None:
    """Verify _filter_devices filters by platform."""
    hosts = {
        "r1": FakeHost(name="r1", hostname="10.0.0.1", platform="eos", groups=[]),
        "r2": FakeHost(name="r2", hostname="10.0.0.2", platform="ios", groups=[]),
    }
    nr = FakeNornir(FakeInventory(FakeHosts(hosts)))
    filtered = server._filter_devices(nr, platform="eos")  # type: ignore[arg-type]
    assert set(filtered.inventory.hosts._hosts.keys()) == {"r1"}  # type: ignore[attr-defined]


def test_list_inventory_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify list_inventory returns empty list when inventory is empty."""

    def mock_init(**_: object) -> FakeNornir:
        return FakeNornir(FakeInventory(FakeHosts({})))

    monkeypatch.setattr("nornir_napalm_mcp.runner.InitNornir", mock_init)
    runner._get_nornir.cache_clear()
    devices = server.nornir_list_inventory()
    assert devices == []
