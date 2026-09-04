"""Tests for introspection.py — NAPALM getter discovery per platform."""

from __future__ import annotations

import pytest

from nornir_mcp import introspection
from tests.conftest import FakeHost, FakeHosts, FakeInventory, FakeNornir


class _BaseStub:
    """Stand-in for napalm.base.NetworkDriver: NotImplementedError stubs."""

    def get_facts(self) -> dict[str, str]:
        raise NotImplementedError

    def get_arp_table(self) -> dict[str, object]:
        raise NotImplementedError

    def get_config(self) -> dict[str, str | None]:
        raise NotImplementedError


class _StubDriver(_BaseStub):
    """Driver overriding one getter; everything else is an inherited stub."""

    def get_facts(self) -> dict[str, str]:
        return {"vendor": "Arista"}


def _patch_nornir(monkeypatch: pytest.MonkeyPatch, hosts: dict[str, FakeHost]) -> None:
    nr = FakeNornir(FakeInventory(FakeHosts(hosts)))
    monkeypatch.setattr(introspection, "get_nornir", lambda: nr)


def _patch_driver(monkeypatch: pytest.MonkeyPatch, driver_cls: type[_BaseStub]) -> None:
    """Patch the discovery-time driver factory and the NetworkDriver base."""
    monkeypatch.setattr(introspection, "NetworkDriver", _BaseStub)

    def _factory(_platform: str) -> type[_BaseStub]:
        return driver_cls

    monkeypatch.setattr("nornir_mcp.introspection.napalm.get_network_driver", _factory)


def test_list_getters_excludes_base_class_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inherited NotImplementedError stubs are not advertised as getters."""
    hosts = {
        "spine-01": FakeHost(name="spine-01", hostname="10.0.0.1", platform="fake_os", groups=[]),
    }
    _patch_nornir(monkeypatch, hosts)
    _patch_driver(monkeypatch, _StubDriver)

    results = introspection.list_getters()
    assert len(results) == 1
    info = results[0]
    assert info.platform == "fake_os"
    # get_facts is overridden; get_arp_table and get_config are base stubs.
    assert info.getters == ["facts"]
    assert info.error is None


def test_introspection_failure_populates_error_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driver introspection failures surface in the error field, not a log."""
    hosts = {
        "spine-01": FakeHost(name="spine-01", hostname="10.0.0.1", platform="fake_os", groups=[]),
    }
    _patch_nornir(monkeypatch, hosts)

    def _fail(platform: str) -> object:
        raise RuntimeError("driver unavailable")

    monkeypatch.setattr(introspection, "NetworkDriver", _BaseStub)
    monkeypatch.setattr("nornir_mcp.introspection.napalm.get_network_driver", _fail)

    results = introspection.list_getters()
    assert len(results) == 1
    assert results[0].platform == "fake_os"
    assert results[0].getters == []
    assert results[0].error == "driver unavailable"


def test_platform_none_host_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts without a platform produce no bogus 'None' platform entry."""
    hosts = {
        "spine-01": FakeHost(name="spine-01", hostname="10.0.0.1", platform="eos", groups=[]),
        "unmanaged": FakeHost(
            name="unmanaged",
            hostname="10.0.0.2",
            platform=None,  # type: ignore[arg-type]
            groups=[],
        ),
    }
    _patch_nornir(monkeypatch, hosts)
    _patch_driver(monkeypatch, _StubDriver)

    results = introspection.list_getters()
    assert [r.platform for r in results] == ["eos"]
