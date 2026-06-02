"""Pytest fixtures: stub Nornir inventory so server.py can be imported
and exercised without a real Nornir config or live network devices."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class _FakeHosts:
    """Minimal mapping implementing the inventory.hosts surface area."""

    def __init__(self, hosts: dict[str, SimpleNamespace]) -> None:
        self._hosts = hosts

    def values(self) -> list[SimpleNamespace]:
        return list(self._hosts.values())

    def get(self, name: str) -> SimpleNamespace | None:
        return self._hosts.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._hosts

    def __iter__(self) -> Any:
        return iter(self._hosts)

    def __len__(self) -> int:
        return len(self._hosts)


class _FakeInventory:
    def __init__(self, hosts: dict[str, SimpleNamespace]) -> None:
        self.hosts = _FakeHosts(hosts)


class _FakeNornir:
    def __init__(self, hosts: dict[str, SimpleNamespace]) -> None:
        self.inventory = _FakeInventory(hosts)

    def filter(self, name: str) -> _FakeNornir:
        return _FakeNornir(
            {name: self.inventory.hosts.get(name)}  # type: ignore[arg-type]
        )

    def run(self, task: Any, getters: list[str]) -> dict[str, list[SimpleNamespace]]:
        name = next(iter(self.inventory.hosts._hosts))
        return {
            name: [
                SimpleNamespace(
                    failed=False,
                    exception=None,
                    result={g: {"ok": True} for g in getters},
                )
            ]
        }


def _make_host(name: str, hostname: str, platform: str, groups: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        hostname=hostname,
        platform=platform,
        groups=[SimpleNamespace(name=g) for g in groups],
    )


@pytest.fixture
def fake_nornir(monkeypatch: pytest.MonkeyPatch) -> dict[str, SimpleNamespace]:
    """Patch server.InitNornir to return a deterministic fake inventory."""
    hosts = {
        "spine-01": _make_host("spine-01", "192.168.1.1", "eos", ["spine", "datacenter-a"]),
        "leaf-01": _make_host("leaf-01", "192.168.1.11", "eos", ["leaf", "datacenter-a"]),
    }
    monkeypatch.setattr("server.InitNornir", lambda **_: _FakeNornir(hosts))
    return hosts
