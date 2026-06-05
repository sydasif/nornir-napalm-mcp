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
    assert data == {
        "spine-01": {"facts": {"hostname": "test-host", "vendor": "Arista", "model": "7280R"}}
    }


def test_run_getter_validates_device_first() -> None:
    """Verify that _run_getter validates device existence before executing tasks."""
    with pytest.raises(ValueError, match="No devices found matching"):
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


def test_run_napalm_getter_raises_on_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that nornir_run_getter raises RuntimeError when getter key is absent."""
    from tests.conftest import FakeTaskResult

    def mock_run(self, **_):
        hosts = self.inventory.hosts._hosts
        name = next(iter(hosts))
        return {name: [FakeTaskResult(result={"other_getter": {"some": "data"}})]}

    monkeypatch.setattr("tests.conftest.FakeNornir.run", mock_run)
    with pytest.raises(RuntimeError, match="unexpected response structure"):
        server.nornir_run_getter("spine-01", "arp_table")


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
    with pytest.raises(ValueError, match="No devices found matching"):
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
    with pytest.raises(ValueError, match="No devices found matching"):
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


# ---------------------------------------------------------------------------
# Error path tests
# ---------------------------------------------------------------------------


def test_run_getter_raises_on_failed_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify _run_getter raises RuntimeError when NAPALM task fails."""
    from tests.conftest import FakeTaskResult

    def mock_run(self, **_):
        hosts = self.inventory.hosts._hosts
        name = next(iter(hosts))
        return {
            name: [
                FakeTaskResult(result={}, failed=True, exception=Exception("Connection refused"))
            ]
        }

    monkeypatch.setattr("tests.conftest.FakeNornir.run", mock_run)
    with pytest.raises(RuntimeError, match="NAPALM task failed"):
        runner._run_getter("spine-01", ["facts"])


def test_get_facts_null_data_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify nornir_get_facts raises RuntimeError when getter returns None."""
    from tests.conftest import FakeTaskResult

    def mock_run(self, **_):
        hosts = self.inventory.hosts._hosts
        name = next(iter(hosts))
        return {name: [FakeTaskResult(result={"facts": None})]}

    monkeypatch.setattr("tests.conftest.FakeNornir.run", mock_run)
    with pytest.raises(RuntimeError, match="returned no data"):
        server.nornir_get_facts("spine-01")


def test_get_facts_wrong_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify nornir_get_facts raises RuntimeError when getter returns non-dict."""
    from tests.conftest import FakeTaskResult

    def mock_run(self, **_):
        hosts = self.inventory.hosts._hosts
        name = next(iter(hosts))
        return {name: [FakeTaskResult(result={"facts": "unexpected_string"})]}

    monkeypatch.setattr("tests.conftest.FakeNornir.run", mock_run)
    with pytest.raises(RuntimeError, match="unexpected type"):
        server.nornir_get_facts("spine-01")


def test_get_config_wrong_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify nornir_get_config raises RuntimeError on non-dict config data."""
    from tests.conftest import FakeTaskResult

    def mock_run(self, **_):
        hosts = self.inventory.hosts._hosts
        name = next(iter(hosts))
        return {name: [FakeTaskResult(result={"config": ["list", "not", "dict"]})]}

    monkeypatch.setattr("tests.conftest.FakeNornir.run", mock_run)
    with pytest.raises(RuntimeError, match="unexpected type"):
        server.nornir_get_config("spine-01")


def test_get_config_null_data_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify nornir_get_config raises RuntimeError when config getter returns None."""
    from tests.conftest import FakeTaskResult

    def mock_run(self, **_):
        hosts = self.inventory.hosts._hosts
        name = next(iter(hosts))
        return {name: [FakeTaskResult(result={"config": None})]}

    monkeypatch.setattr("tests.conftest.FakeNornir.run", mock_run)
    with pytest.raises(RuntimeError, match="returned no data"):
        server.nornir_get_config("spine-01")


def test_list_getters_unknown_platform_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify list_getters returns empty getters for unknown platform."""
    hosts_data = {
        "bogus": FakeHost(name="bogus", hostname="10.0.0.1", platform="nonexistent_os", groups=[]),
    }

    def mock_init(**_):
        return FakeNornir(FakeInventory(FakeHosts(hosts_data)))

    monkeypatch.setattr("runner.InitNornir", mock_init)
    runner._nornir = None
    server._getters_cache = None
    results = server.nornir_list_getters()
    assert len(results) == 1
    assert results[0].platform == "nonexistent_os"
    assert results[0].getters == []


def test_run_cli_rejects_whitespace_only_command() -> None:
    """Verify run_cli rejects command that is only whitespace."""
    with pytest.raises(ValueError, match="not a read-only show command"):
        server.nornir_run_cli("spine-01", ["   "])


def test_list_inventory_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify list_inventory returns empty list when inventory is empty."""

    def mock_init(**_):
        return FakeNornir(FakeInventory(FakeHosts({})))

    monkeypatch.setattr("runner.InitNornir", mock_init)
    runner._nornir = None
    devices = server.nornir_list_inventory()
    assert devices == []


def test_reset_nornir_returns_previous() -> None:
    """Verify reset_nornir returns the previous instance and clears the singleton."""
    runner._get_nornir()
    previous = runner.reset_nornir()
    assert previous is not None
    assert runner._nornir is None


def test_getters_cache_cleared_on_reload() -> None:
    """Verify getters cache is cleared after inventory reload."""
    server.nornir_list_getters()
    assert server._getters_cache is not None
    server.nornir_reload_inventory()
    assert server._getters_cache is None


def test_getters_cache_hit_returns_cached() -> None:
    """Verify second call to list_getters returns cached result."""
    # Ensure fresh state then call twice within the same test
    runner._get_nornir()  # populate singleton
    server._getters_cache = None
    first = server.nornir_list_getters()
    assert server._getters_cache is not None
    second = server.nornir_list_getters()
    assert first is second


def test_network_facts_coerces_int_to_str() -> None:
    """Verify NetworkFacts field_validator coerces non-string values to str."""
    from models import NetworkFacts

    facts = NetworkFacts(hostname=123, vendor=None, model=True)  # type: ignore[arg-type]
    assert facts.hostname == "123"
    assert facts.vendor is None
    assert facts.model == "True"


def test_reset_nornir_when_already_none() -> None:
    """Verify reset_nornir returns None when singleton is not initialised."""
    runner._nornir = None
    previous = runner.reset_nornir()
    assert previous is None


def test_extract_multiple_result_empty_host_result() -> None:
    """Verify _extract_multiple_result raises on empty MultiResult."""
    with pytest.raises(RuntimeError, match="Empty result"):
        runner._extract_multiple_result({"spine-01": []})


def test_extract_multiple_result_success() -> None:
    """Verify _extract_multiple_result extracts results from multiple hosts."""
    from tests.conftest import FakeTaskResult

    result = {
        "spine-01": [FakeTaskResult(result={"facts": {"hostname": "spine-01"}})],
        "leaf-01": [FakeTaskResult(result={"facts": {"hostname": "leaf-01"}})],
    }
    extracted = runner._extract_multiple_result(result)
    assert set(extracted.keys()) == {"spine-01", "leaf-01"}
    assert extracted["spine-01"]["facts"]["hostname"] == "spine-01"


# ---------------------------------------------------------------------------
# Batch operation tests
# ---------------------------------------------------------------------------


def test_run_getter_batch_returns_dict() -> None:
    """Verify _run_getter with list returns dict keyed by device name."""
    data = runner._run_getter(["spine-01", "leaf-01"], ["facts"])
    assert isinstance(data, dict)
    assert set(data.keys()) == {"spine-01", "leaf-01"}
    for dev_data in data.values():
        assert "facts" in dev_data


def test_run_getter_batch_validates_all_devices() -> None:
    """Verify _run_getter batch raises ValueError if any device not found."""
    with pytest.raises(ValueError, match="Following devices not found in inventory"):
        runner._run_getter(["spine-01", "nonexistent"], ["facts"])


def test_run_cli_batch_returns_dict() -> None:
    """Verify _run_cli with list returns dict keyed by device name."""
    data = runner._run_cli(["spine-01", "leaf-01"], ["show version"])
    assert isinstance(data, dict)
    assert set(data.keys()) == {"spine-01", "leaf-01"}
    for dev_data in data.values():
        assert "show version" in dev_data


def test_nornir_get_facts_batch() -> None:
    """Verify nornir_get_facts with list returns dict of NetworkFacts."""
    result = server.nornir_get_facts(["spine-01", "leaf-01"])
    assert isinstance(result, dict)
    assert set(result.keys()) == {"spine-01", "leaf-01"}
    for facts in result.values():
        assert facts.hostname == "test-host"
        assert facts.vendor == "Arista"


def test_nornir_get_interfaces_batch() -> None:
    """Verify nornir_get_interfaces with list returns dict of NetworkInterfaces."""
    result = server.nornir_get_interfaces(["spine-01", "leaf-01"])
    assert isinstance(result, dict)
    assert set(result.keys()) == {"spine-01", "leaf-01"}
    for interfaces in result.values():
        assert "Ethernet1" in interfaces.interfaces


def test_nornir_run_getter_batch() -> None:
    """Verify nornir_run_getter with list returns dict of results."""
    result = server.nornir_run_getter(["spine-01", "leaf-01"], "arp_table")
    assert isinstance(result, dict)
    assert set(result.keys()) == {"spine-01", "leaf-01"}
    for dev_result in result.values():
        assert dev_result == {"ok": True}


def test_nornir_get_config_batch() -> None:
    """Verify nornir_get_config with list returns dict of DeviceConfig."""
    result = server.nornir_get_config(["spine-01", "leaf-01"])
    assert isinstance(result, dict)
    assert set(result.keys()) == {"spine-01", "leaf-01"}
    for cfg in result.values():
        assert cfg.running is not None
        assert cfg.startup is not None


def test_nornir_run_cli_batch() -> None:
    """Verify nornir_run_cli with list returns dict of command outputs."""
    result = server.nornir_run_cli(["spine-01", "leaf-01"], ["show version"])
    assert isinstance(result, dict)
    assert set(result.keys()) == {"spine-01", "leaf-01"}
    for dev_result in result.values():
        assert "show version" in dev_result


def test_list_inventory_filter_by_platform() -> None:
    """Verify list_inventory filters by platform."""
    devices = server.nornir_list_inventory(platform="eos")
    assert len(devices) == 2
    assert all(d.platform == "eos" for d in devices)


def test_list_inventory_filter_by_platform_no_match() -> None:
    """Verify list_inventory returns empty when platform doesn't match."""
    devices = server.nornir_list_inventory(platform="ios")
    assert devices == []


def test_list_inventory_filter_by_group() -> None:
    """Verify list_inventory filters by group."""
    devices = server.nornir_list_inventory(group="spine")
    assert len(devices) == 1
    assert devices[0].name == "spine-01"


def test_list_inventory_filter_by_group_no_match() -> None:
    """Verify list_inventory returns empty when group doesn't match."""
    devices = server.nornir_list_inventory(group="nonexistent")
    assert devices == []


def test_list_inventory_filter_by_both() -> None:
    """Verify list_inventory filters by both group and platform."""
    devices = server.nornir_list_inventory(group="leaf", platform="eos")
    assert len(devices) == 1
    assert devices[0].name == "leaf-01"


# ---------------------------------------------------------------------------
# nornir_run_ping
# ---------------------------------------------------------------------------


def test_run_ping_success() -> None:
    """Verify nornir_run_ping returns PingResult with stats on success."""
    result = server.nornir_run_ping(dest="10.0.0.1", device_name="spine-01")
    assert isinstance(result, dict) is False
    assert result.destination == "10.0.0.1"
    assert result.success is True
    assert result.stats is not None
    assert result.stats.packets_sent == 5
    assert result.stats.packets_received == 5
    assert result.error is None


def test_run_ping_batch() -> None:
    """Verify nornir_run_ping with list returns dict of PingResults."""
    result = server.nornir_run_ping(dest="10.0.0.1", device_name=["spine-01", "leaf-01"])
    assert isinstance(result, dict)
    assert set(result.keys()) == {"spine-01", "leaf-01"}
    for ping in result.values():
        assert ping.success is True
        assert ping.stats is not None


def test_run_ping_unknown_device() -> None:
    """Verify nornir_run_ping raises ValueError for unknown device."""
    with pytest.raises(ValueError, match="No devices found matching"):
        server.nornir_run_ping(dest="10.0.0.1", device_name="nonexistent")


def test_run_ping_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify nornir_run_ping returns PingResult with error on failure."""
    from tests.conftest import FakeTaskResult

    def mock_run(self, **_):
        hosts = self.inventory.hosts._hosts
        name = next(iter(hosts))
        return {name: [FakeTaskResult(result={"error": "Timeout"})]}

    monkeypatch.setattr("tests.conftest.FakeNornir.run", mock_run)
    runner._nornir = None
    result = server.nornir_run_ping(dest="10.0.0.1", device_name="spine-01")
    assert result.success is False
    assert result.error == "Timeout"
    assert result.stats is None


def test_ping_stats_auto_compute_loss() -> None:
    """Verify PingStats computes packet_loss when omitted."""
    from models import PingStats

    stats = PingStats(packets_sent=10, packets_received=7)
    assert stats.packet_loss == 30.0


def test_ping_stats_explicit_loss_preserved() -> None:
    """Verify PingStats preserves explicit packet_loss value."""
    from models import PingStats

    stats = PingStats(packets_sent=10, packets_received=7, packet_loss=50.0)
    assert stats.packet_loss == 50.0


def test_ping_result_parse_napalm_stats() -> None:
    """Verify PingResult parses NAPALM-style success dict into PingStats."""
    from models import PingResult

    result = PingResult(
        destination="10.0.0.1",
        success=True,
        stats={
            "packets_sent": 5,
            "packets_received": 5,
            "rtt_min": 1.0,
            "rtt_max": 3.0,
            "rtt_avg": 2.0,
        },
    )
    assert result.stats is not None
    assert result.stats.rtt_min == 1.0
    assert result.stats.rtt_avg == 2.0
    assert result.stats.packets_sent == 5


def test_ping_result_no_stats_on_failure() -> None:
    """Verify PingResult has no stats when NAPALM returns error dict."""
    from models import PingResult

    result = PingResult(destination="10.0.0.1", success=False, error="unreachable")
    assert result.stats is None
    assert result.error == "unreachable"


# ---------------------------------------------------------------------------
# nornir_run_getter enhanced options
# ---------------------------------------------------------------------------


def test_run_getter_with_options() -> None:
    """Verify nornir_run_getter passes getter_options through."""
    result = server.nornir_run_getter("spine-01", "facts", getter_options={"keys": ["hostname"]})
    assert result == {"hostname": "test-host", "vendor": "Arista", "model": "7280R"}


def test_run_getter_with_timeout() -> None:
    """Verify nornir_run_getter passes timeout through."""
    result = server.nornir_run_getter("spine-01", "facts", timeout=30)
    assert result == {"hostname": "test-host", "vendor": "Arista", "model": "7280R"}


def test_run_getter_batch_with_options() -> None:
    """Verify nornir_run_getter batch with getter_options."""
    result = server.nornir_run_getter(
        ["spine-01", "leaf-01"], "facts", getter_options={"keys": ["hostname"]}
    )
    assert isinstance(result, dict)
    assert set(result.keys()) == {"spine-01", "leaf-01"}
