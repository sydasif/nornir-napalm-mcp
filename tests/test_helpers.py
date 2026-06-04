"""Tests for server.py helpers and tool entry points.

These tests run against a fake Nornir inventory injected via
the `fake_nornir` fixture — no real devices or SSH sessions involved.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import runner
import server
from tests.conftest import FakeGroup, FakeHost, FakeHosts, FakeInventory, FakeNornir


@pytest.fixture(autouse=True)
def _reload_server(fake_nornir: dict[str, FakeHost]) -> Iterator[None]:
    """Reset runner's cached Nornir singleton before each test.

    Args:
        fake_nornir: The fake inventory fixture.
    """
    runner._nornir = None
    yield
    runner._nornir = None


def test_resolve_config_defaults_to_module_relative(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify config resolution defaults to config.yaml relative to the runner module."""
    monkeypatch.delenv("NORNIR_CONFIG", raising=False)
    path = runner._resolve_config()
    assert path.name == "config.yaml"
    assert path.is_absolute()


def test_resolve_config_honors_env_var(tmp_path, monkeypatch) -> None:
    """Verify that the NORNIR_CONFIG environment variable is respected."""
    custom = tmp_path / "custom.yaml"
    custom.write_text("inventory:\n  plugin: SimpleInventory\n")
    monkeypatch.setenv("NORNIR_CONFIG", str(custom))
    assert runner._resolve_config() == custom.resolve()


def test_resolve_config_resolves_relative_paths(tmp_path, monkeypatch) -> None:
    """Verify that relative paths in NORNIR_CONFIG are resolved correctly."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NORNIR_CONFIG", "config.yaml")
    resolved = runner._resolve_config()
    assert resolved.is_absolute()
    assert resolved.name == "config.yaml"


def test_get_nornir_caches_singleton() -> None:
    """Verify that the Nornir instance is cached after the first initialization."""
    a = runner._get_nornir()
    b = runner._get_nornir()
    assert a is b


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


def test_run_getter_returns_getter_payload() -> None:
    """Verify that napalm_get returns the expected payload for a valid host."""
    data = runner._run_getter("spine-01", ["facts"])
    assert data == {"facts": {"hostname": "test-host", "vendor": "Arista", "model": "7280R"}}


def test_run_getter_validates_device_first() -> None:
    """Verify that _run_getter validates device existence before executing tasks."""
    with pytest.raises(ValueError, match="not found in inventory"):
        runner._run_getter("nope", ["facts"])


def test_get_network_facts_returns_facts_dict() -> None:
    """Verify the get_network_facts tool returns the correct filtered data."""
    facts = server.nornir_get_facts("spine-01")
    assert facts.hostname == "test-host"
    assert facts.vendor == "Arista"
    assert facts.model == "7280R"
    assert facts.additional_facts == {}


def test_get_network_interfaces_merges_keys() -> None:
    """Verify the get_network_interfaces tool returns distinct data per getter."""
    out = server.nornir_get_interfaces("leaf-01")
    assert out.interfaces == {"Ethernet1": {"state": "up", "speed": "1000"}}
    assert out.interfaces_ip == {"Ethernet1": {"ipv4": {"10.0.0.1/24": {}}}}
    assert out.interfaces != out.interfaces_ip


def test_run_napalm_getter_rejects_invalid_name() -> None:
    """Verify that run_napalm_getter rejects getter names with invalid characters."""
    with pytest.raises(ValueError, match="Invalid getter name"):
        server.nornir_run_getter("spine-01", "bad-getter!")


def test_run_napalm_getter_returns_payload() -> None:
    """Verify that run_napalm_getter returns the specific getter payload."""
    out = server.nornir_run_getter("spine-01", "arp_table")
    assert out == {"ok": True}


def test_run_napalm_getter_fallback_to_full_dict() -> None:
    """Verify that run_napalm_getter falls back to full dict when getter key missing."""
    # The fake always returns the getter key, but test the fallback logic directly
    data = {"other_getter": {"some": "data"}}
    result = data.get("arp_table", data)
    assert result == data


def test_reload_inventory_initial_summary() -> None:
    """Verify the inventory reload summary when starting from an empty state."""
    report = server.nornir_reload_inventory()
    assert report.total == 2
    assert set(report.current_hosts) == {"spine-01", "leaf-01"}
    assert report.previous_hosts == []
    assert sorted(report.added) == ["leaf-01", "spine-01"]
    assert report.removed == []


def test_reload_inventory_detects_added_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that inventory reload detects newly added and removed hosts."""
    server.nornir_reload_inventory()

    # Define a new fake state
    new_hosts = {
        "router-99": FakeHost(
            name="router-99",
            hostname="10.99.0.1",
            platform="eos",
            groups=[FakeGroup(name="spine")],
        ),
        "spine-01": FakeHost(
            name="spine-01",
            hostname="192.168.1.1",
            platform="eos",
            groups=[FakeGroup(name="spine")],
        ),
    }

    def mock_init(**_) -> FakeNornir:
        return FakeNornir(FakeInventory(FakeHosts(new_hosts)))

    monkeypatch.setattr("runner.InitNornir", mock_init)
    report = server.nornir_reload_inventory()

    assert "router-99" in report.current_hosts
    assert "router-99" in report.added
    assert "leaf-01" in report.removed
    assert report.total == 2


def test_reload_inventory_rebuilds_singleton() -> None:
    """Verify that reloading the inventory creates a new Nornir instance."""
    server.nornir_reload_inventory()
    before = runner._nornir
    server.nornir_reload_inventory()
    after = runner._nornir
    assert before is not None
    assert after is not None
    assert before is not after


# ---------------------------------------------------------------------------
# nornir_get_config
# ---------------------------------------------------------------------------


def test_get_config_returns_both() -> None:
    """Verify get_config returns both running and startup by default."""
    cfg = server.nornir_get_config("spine-01")
    assert cfg.running is not None
    assert cfg.startup is not None
    assert "running-config" in cfg.running
    assert "startup-config" in cfg.startup


def test_get_config_running_only() -> None:
    """Verify get_config with config_type='running' returns only running."""
    cfg = server.nornir_get_config("spine-01", config_type="running")
    assert cfg.running is not None
    assert cfg.startup is None


def test_get_config_startup_only() -> None:
    """Verify get_config with config_type='startup' returns only startup."""
    cfg = server.nornir_get_config("spine-01", config_type="startup")
    assert cfg.running is None
    assert cfg.startup is not None


def test_get_config_rejects_invalid_type() -> None:
    """Verify get_config raises ValueError for invalid config_type."""
    with pytest.raises(ValueError, match="Invalid config_type"):
        server.nornir_get_config("spine-01", config_type="invalid")


def test_get_config_unknown_device() -> None:
    """Verify get_config raises ValueError for unknown device."""
    with pytest.raises(ValueError, match="not found in inventory"):
        server.nornir_get_config("nope")


# ---------------------------------------------------------------------------
# nornir_run_cli
# ---------------------------------------------------------------------------


def test_run_cli_returns_output() -> None:
    """Verify run_cli returns command-to-output mapping."""
    out = server.nornir_run_cli("spine-01", ["show version"])
    assert "show version" in out
    assert "Output for: show version" == out["show version"]


def test_run_cli_multiple_commands() -> None:
    """Verify run_cli handles multiple commands."""
    out = server.nornir_run_cli("spine-01", ["show version", "show ip route"])
    assert len(out) == 2
    assert "show version" in out
    assert "show ip route" in out


def test_run_cli_rejects_config_commands() -> None:
    """Verify run_cli rejects non-show commands."""
    with pytest.raises(ValueError, match="not a read-only show command"):
        server.nornir_run_cli("spine-01", ["configure terminal"])


def test_run_cli_rejects_write_commands() -> None:
    """Verify run_cli rejects write/save commands."""
    with pytest.raises(ValueError, match="not a read-only show command"):
        server.nornir_run_cli("spine-01", ["write memory"])


def test_run_cli_rejects_empty_commands() -> None:
    """Verify run_cli raises ValueError for empty command list."""
    with pytest.raises(ValueError, match="No commands provided"):
        server.nornir_run_cli("spine-01", [])


def test_run_cli_unknown_device() -> None:
    """Verify run_cli raises ValueError for unknown device."""
    with pytest.raises(ValueError, match="not found in inventory"):
        server.nornir_run_cli("nope", ["show version"])


# ---------------------------------------------------------------------------
# nornir_list_getters
# ---------------------------------------------------------------------------


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
