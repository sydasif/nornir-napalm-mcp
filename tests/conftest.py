"""Pytest fixtures: stub Nornir inventory so server.py can be imported
and exercised without a real Nornir config or live network devices."""

from __future__ import annotations

from collections.abc import Iterator
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

    def keys(self) -> list[str]:
        return list(self._hosts.keys())

    def __iter__(self) -> Iterator[str]:
        return iter(self._hosts)

    def __len__(self) -> int:
        return len(self._hosts)


@dataclass
class FakeInventory:
    """Stub for Nornir Inventory."""

    hosts: FakeHosts


@dataclass
class FakeTaskResult:
    """Stub for Nornir TaskResult."""

    result: dict[str, Any]
    failed: bool = False
    exception: Any = None


@dataclass
class FakeNornir:
    """Stub for Nornir instance."""

    inventory: FakeInventory

    def filter(self, name: str | None = None, name__in: list[str] | None = None) -> FakeNornir:
        """Filter hosts by name or list of names."""
        if name__in is not None:
            filtered = {k: v for k, v in self.inventory.hosts._hosts.items() if k in name__in}
            return FakeNornir(FakeInventory(FakeHosts(filtered)))
        if name is not None:
            host = self.inventory.hosts.get(name)
            return FakeNornir(FakeInventory(FakeHosts({name: host}) if host else FakeHosts({})))
        return self

    def run(self, task: Any, **kwargs: Any) -> dict[str, list[Any]]:
        """Run a task against all hosts in the filtered inventory."""
        hosts = self.inventory.hosts._hosts
        if not hosts:
            return {}

        # Dispatch based on which kwargs are present
        if "getters" in kwargs:
            getters = kwargs["getters"]
            payloads = {
                "facts": {"hostname": "test-host", "vendor": "Arista", "model": "7280R"},
                "interfaces": {"Ethernet1": {"state": "up", "speed": "1000"}},
                "interfaces_ip": {"Ethernet1": {"ipv4": {"10.0.0.1/24": {}}}},
                "config": {
                    "running": "! running-config\nhostname test-host\n",
                    "startup": "! startup-config\nhostname test-host\n",
                },
            }
            result = {g: payloads.get(g, {"ok": True}) for g in getters}
            return {name: [FakeTaskResult(result)] for name in hosts}

        if "commands" in kwargs:
            commands = kwargs["commands"]
            result = {cmd: f"Output for: {cmd}" for cmd in commands}
            return {name: [FakeTaskResult(result)] for name in hosts}

        return {}


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

    monkeypatch.setattr("runner.InitNornir", mock_init)
    return hosts_data
