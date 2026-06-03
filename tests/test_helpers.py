"""Tests for server.py helpers and tool entry points.

These tests run against a fake Nornir inventory injected via
the `fake_nornir` fixture — no real devices or SSH sessions involved.
"""

from __future__ import annotations

import pytest

import server
from tests.conftest import FakeGroup, FakeHost, FakeHosts, FakeInventory, FakeNornir


@pytest.fixture(autouse=True)
def _reload_server(fake_nornir) -> None:
    """Reset server's cached Nornir singleton before each test.

    Args:
        fake_nornir: The fake inventory fixture.
    """
    server._nornir = None
    yield
    server._nornir = None


def test_get_host_returns_known_host() -> None:
    """Verify that a known device name returns the correct Host object."""
    host = server._get_host("spine-01")
    assert isinstance(host, FakeHost)
    assert host.name == "spine-01"
    assert host.platform == "eos"


def test_get_host_raises_for_unknown_device() -> None:
    """Verify that requesting a non-existent device raises a ValueError."""
    with pytest.raises(ValueError, match="not found in inventory"):
        server._get_host("does-not-exist")


def test_resolve_config_defaults_to_module_relative() -> None:
    """Verify config resolution defaults to config.yaml relative to the server module."""
    import os

    old = os.environ.pop("NORNIR_CONFIG", None)
    try:
        path = server._resolve_config()
        assert path.name == "config.yaml"
        assert path.is_absolute()
    finally:
        if old is not None:
            os.environ["NORNIR_CONFIG"] = old


def test_resolve_config_honors_env_var(tmp_path, monkeypatch) -> None:
    """Verify that the NORNIR_CONFIG environment variable is respected."""
    custom = tmp_path / "custom.yaml"
    custom.write_text("inventory:\n  plugin: SimpleInventory\n")
    monkeypatch.setenv("NORNIR_CONFIG", str(custom))
    assert server._resolve_config() == custom.resolve()


def test_resolve_config_resolves_relative_paths(tmp_path, monkeypatch) -> None:
    """Verify that relative paths in NORNIR_CONFIG are resolved correctly."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NORNIR_CONFIG", "config.yaml")
    resolved = server._resolve_config()
    assert resolved.is_absolute()
    assert resolved.name == "config.yaml"


def test_get_nornir_caches_singleton() -> None:
    """Verify that the Nornir instance is cached after the first initialization."""
    a = server._get_nornir()
    b = server._get_nornir()
    assert a is b


def test_list_inventory_shape() -> None:
    """Verify the structure and content of the inventory list."""
    devices = server.nornir_list_inventory()
    assert isinstance(devices, list)
    assert {d["name"] for d in devices} == {"spine-01", "leaf-01"}
    sample = devices[0]
    assert set(sample.keys()) == {"name", "hostname", "platform", "groups"}
    assert isinstance(sample["groups"], list)


def test_list_inventory_sorted() -> None:
    """Verify that the inventory list is returned sorted by device name."""
    devices = server.nornir_list_inventory()
    names = [d["name"] for d in devices]
    assert names == sorted(names)


def test_run_getter_returns_getter_payload() -> None:
    """Verify that napalm_get returns the expected payload for a valid host."""
    data = server._run_getter("spine-01", ["facts"])
    assert data == {"facts": {"ok": True}}


def test_run_getter_validates_device_first() -> None:
    """Verify that _run_getter validates device existence before executing tasks."""
    with pytest.raises(ValueError, match="not found in inventory"):
        server._run_getter("nope", ["facts"])


def test_get_network_facts_returns_facts_dict() -> None:
    """Verify the get_network_facts tool returns the correct filtered data."""
    facts = server.nornir_get_facts("spine-01")
    assert facts == {"ok": True}


def test_get_network_interfaces_merges_keys() -> None:
    """Verify the get_network_interfaces tool returns the merged interface data."""
    out = server.nornir_get_interfaces("leaf-01")
    assert set(out.keys()) == {"interfaces", "interfaces_ip"}
    assert out["interfaces"] == {"ok": True}
    assert out["interfaces_ip"] == {"ok": True}


def test_run_napalm_getter_rejects_invalid_name() -> None:
    """Verify that run_napalm_getter rejects getter names with invalid characters."""
    with pytest.raises(ValueError, match="Invalid getter name"):
        server.nornir_run_getter("spine-01", "bad-getter!")


def test_run_napalm_getter_returns_payload() -> None:
    """Verify that run_napalm_getter returns the specific getter payload."""
    out = server.nornir_run_getter("spine-01", "arp_table")
    assert out == {"ok": True}


def test_reload_inventory_initial_summary() -> None:
    """Verify the inventory reload summary when starting from an empty state."""
    report = server.nornir_reload_inventory()
    assert report["total"] == 2
    assert set(report["current_hosts"]) == {"spine-01", "leaf-01"}
    assert report["previous_hosts"] == []
    assert sorted(report["added"]) == ["leaf-01", "spine-01"]
    assert report["removed"] == []


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

    monkeypatch.setattr("server.InitNornir", mock_init)
    report = server.nornir_reload_inventory()

    assert "router-99" in report["current_hosts"]
    assert "router-99" in report["added"]
    assert "leaf-01" in report["removed"]
    assert report["total"] == 2


def test_reload_inventory_rebuilds_singleton() -> None:
    """Verify that reloading the inventory creates a new Nornir instance."""
    server.nornir_reload_inventory()
    before = server._nornir
    server.nornir_reload_inventory()
    after = server._nornir
    assert before is not None
    assert after is not None
    assert before is not after
