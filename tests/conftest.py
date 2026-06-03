"""Pytest fixtures: stub Nornir inventory so server.py can be imported
and exercised without a real Nornir config or live network devices."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class FakeGroup:
    """Stub for Nornir Group."""

    name: str


@dataclass
class FakeHost:
    """Stub for Nornir Host."""

    name: str
    hostname: str
    platform: str
    groups: list[FakeGroup] = field(default_factory=list)


@dataclass
class FakeHosts:
    """Minimal mapping implementing the inventory.hosts surface area."""

    _hosts: dict[str, FakeHost]

    def values(self) -> list[FakeHost]:
        return list(self._hosts.values())

    def get(self, name: str) -> FakeHost | None:
        return self._hosts.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._hosts

    def __iter__(self) -> Any:
        return iter(self._hosts)

    def __len__(self) -> int:
        return len(self._hosts)


@dataclass
class FakeInventory:
    """Stub for Nornir Inventory."""

    hosts: FakeHosts


@dataclass
class FakeNornir:
    """Stub for Nornir instance."""

    inventory: FakeInventory

    def filter(self, name: str) -> FakeNornir:
        host = self.inventory.hosts.get(name)
        return FakeNornir(FakeInventory(FakeHosts({name: host}) if host else FakeHosts({})))

    def run(self, task: Any, getters: list[str]) -> dict[str, list[Any]]:
        # Get the first host name from the filtered inventory
        hosts = self.inventory.hosts._hosts
        if not hosts:
            return {}

        name = next(iter(hosts))

        # Mock TaskResult
        class TaskResult:
            def __init__(self, result: dict[str, Any]):
                self.failed = False
                self.exception = None
                self.result = result

        return {name: [TaskResult({g: {"ok": True} for g in getters})]}


def _make_host(name: str, hostname: str, platform: str, groups: list[str]) -> FakeHost:
    return FakeHost(
        name=name,
        hostname=hostname,
        platform=platform,
        groups=[FakeGroup(name=g) for g in groups],
    )


@pytest.fixture
def fake_nornir(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeHost]:
    """Patch server.InitNornir to return a deterministic fake inventory."""
    hosts_data = {
        "spine-01": _make_host("spine-01", "192.168.1.1", "eos", ["spine", "datacenter-a"]),
        "leaf-01": _make_host("leaf-01", "192.168.1.11", "eos", ["leaf", "datacenter-a"]),
    }

    def mock_init(**_) -> FakeNornir:
        return FakeNornir(FakeInventory(FakeHosts(hosts_data)))

    monkeypatch.setattr("server.InitNornir", mock_init)
    return hosts_data
