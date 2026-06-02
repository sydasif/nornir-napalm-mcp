"""Tests for server.py helpers and tool entry points.

These tests run against a fake Nornir inventory injected via
the `fake_nornir` fixture — no real devices or SSH sessions involved.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import server


@pytest.fixture(autouse=True)
def _reload_server(fake_nornir):
    """Reset server's cached Nornir singleton before each test."""
    server._nornir = None
    yield
    server._nornir = None


def test_get_host_returns_known_host() -> None:
    host = server._get_host("spine-01")
    assert host.name == "spine-01"
    assert host.platform == "eos"


def test_get_host_raises_for_unknown_device() -> None:
    with pytest.raises(ValueError, match="not found in inventory"):
        server._get_host("does-not-exist")


def test_resolve_config_defaults_to_module_relative() -> None:
    import os

    old = os.environ.pop("NORNIR_CONFIG", None)
    try:
        path = server._resolve_config()
        assert path.endswith("config.yaml")
        assert "/net-tool/" in path or path.startswith("/")
    finally:
        if old is not None:
            os.environ["NORNIR_CONFIG"] = old


def test_resolve_config_honors_env_var(tmp_path, monkeypatch) -> None:
    custom = tmp_path / "custom.yaml"
    custom.write_text("inventory:\n  plugin: SimpleInventory\n")
    monkeypatch.setenv("NORNIR_CONFIG", str(custom))
    assert server._resolve_config() == str(custom)


def test_resolve_config_resolves_relative_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NORNIR_CONFIG", "config.yaml")
    resolved = server._resolve_config()
    assert resolved.startswith("/")
    assert resolved.endswith("config.yaml")


def test_get_nornir_caches_singleton() -> None:
    a = server._get_nornir()
    b = server._get_nornir()
    assert a is b


def test_list_inventory_shape() -> None:
    devices = server.list_inventory()
    assert isinstance(devices, list)
    assert {d["name"] for d in devices} == {"spine-01", "leaf-01"}
    sample = devices[0]
    assert set(sample.keys()) == {"name", "hostname", "platform", "groups"}
    assert isinstance(sample["groups"], list)


def test_list_inventory_sorted() -> None:
    devices = server.list_inventory()
    names = [d["name"] for d in devices]
    assert names == sorted(names)


def test_run_getter_returns_getter_payload() -> None:
    data = server._run_getter("spine-01", ["facts"])
    assert data == {"facts": {"ok": True}}


def test_run_getter_validates_device_first() -> None:
    with pytest.raises(ValueError, match="not found in inventory"):
        server._run_getter("nope", ["facts"])


def test_get_network_facts_returns_facts_dict() -> None:
    facts = server.get_network_facts("spine-01")
    assert facts == {"ok": True}


def test_get_network_interfaces_merges_keys() -> None:
    out = server.get_network_interfaces("leaf-01")
    assert set(out.keys()) == {"interfaces", "interfaces_ip"}
    assert out["interfaces"] == {"ok": True}
    assert out["interfaces_ip"] == {"ok": True}


def test_run_napalm_getter_rejects_invalid_name() -> None:
    with pytest.raises(ValueError, match="Invalid getter name"):
        server.run_napalm_getter("spine-01", "bad-getter!")


def test_run_napalm_getter_returns_payload() -> None:
    out = server.run_napalm_getter("spine-01", "arp_table")
    assert out == {"ok": True}


def test_reload_inventory_initial_summary() -> None:
    report = server.reload_inventory()
    assert report["total"] == 2
    assert set(report["current_hosts"]) == {"spine-01", "leaf-01"}
    assert report["previous_hosts"] == []
    assert sorted(report["added"]) == ["leaf-01", "spine-01"]
    assert report["removed"] == []


def test_reload_inventory_detects_added_host(monkeypatch: pytest.MonkeyPatch) -> None:
    server.reload_inventory()
    new_fake = SimpleNamespace(
        inventory=SimpleNamespace(
            hosts={
                "router-99": SimpleNamespace(
                    name="router-99",
                    hostname="10.99.0.1",
                    platform="eos",
                    groups=[SimpleNamespace(name="spine")],
                ),
                "spine-01": SimpleNamespace(
                    name="spine-01",
                    hostname="192.168.1.1",
                    platform="eos",
                    groups=[SimpleNamespace(name="spine")],
                ),
            }
        )
    )
    monkeypatch.setattr("server.InitNornir", lambda **_: new_fake)
    report = server.reload_inventory()
    assert "router-99" in report["current_hosts"]
    assert "router-99" in report["added"]
    assert "leaf-01" in report["removed"]
    assert report["total"] == 2


def test_reload_inventory_rebuilds_singleton() -> None:
    server.reload_inventory()
    before = server._nornir
    server.reload_inventory()
    after = server._nornir
    assert before is not None
    assert after is not None
    assert before is not after
