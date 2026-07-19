"""Tests for tasks.py — device filtering and task execution helpers."""

from __future__ import annotations

import pytest

from nornir_napalm_mcp.tasks import _filter_devices
from tests.conftest import FakeGroup, FakeHost, FakeHosts, FakeInventory, FakeNornir


def test_filter_devices_empty_raises() -> None:
    """Verify _filter_devices raises ValueError when no devices match."""
    nr = FakeNornir(FakeInventory(FakeHosts({})))
    with pytest.raises(ValueError, match="No devices match the provided filters"):
        _filter_devices(nr, name="nonexistent")


def test_filter_devices_by_name_list() -> None:
    """Verify _filter_devices filters by list of names."""
    hosts = {
        "a": FakeHost(name="a", hostname="10.0.0.1", platform="eos", groups=[]),
        "b": FakeHost(name="b", hostname="10.0.0.2", platform="eos", groups=[]),
    }
    nr = FakeNornir(FakeInventory(FakeHosts(hosts)))
    filtered = _filter_devices(nr, name=["a"])
    assert set(filtered.inventory.hosts._hosts.keys()) == {"a"}


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
    filtered = _filter_devices(nr, group="core")
    assert set(filtered.inventory.hosts._hosts.keys()) == {"r1"}


def test_filter_devices_by_platform() -> None:
    """Verify _filter_devices filters by platform."""
    hosts = {
        "r1": FakeHost(name="r1", hostname="10.0.0.1", platform="eos", groups=[]),
        "r2": FakeHost(name="r2", hostname="10.0.0.2", platform="ios", groups=[]),
    }
    nr = FakeNornir(FakeInventory(FakeHosts(hosts)))
    filtered = _filter_devices(nr, platform="eos")
    assert set(filtered.inventory.hosts._hosts.keys()) == {"r1"}
