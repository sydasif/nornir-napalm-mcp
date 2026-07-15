"""Pytest fixtures: stub Nornir inventory so server.py can be imported
and exercised without a real Nornir config or live network devices."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# Create a sentinel config file so runner._resolve_config_path succeeds.
_TEST_CONFIG = Path("/tmp/nornir_test_config.yaml")
_TEST_CONFIG.touch(exist_ok=True)


@dataclass(slots=True)
class FakeGroup:
    """Stub for Nornir Group."""

    name: str


@dataclass(slots=True)
class FakeHost:
    """Stub for Nornir Host."""

    name: str
    hostname: str
    platform: str
    groups: list[FakeGroup] = field(default_factory=list)


@dataclass(slots=True)
class FakeHosts:
    """Minimal mapping implementing the inventory.hosts surface area."""

    _hosts: dict[str, FakeHost]

    def values(self) -> list[FakeHost]:
        return list(self._hosts.values())

    def __iter__(self) -> Iterator[str]:
        return iter(self._hosts)

    def __len__(self) -> int:
        return len(self._hosts)


@dataclass(slots=True)
class FakeInventory:
    """Stub for Nornir Inventory."""

    hosts: FakeHosts


@dataclass(slots=True)
class FakeTaskResult:
    """Stub for Nornir TaskResult."""

    result: dict[str, Any]
    failed: bool = False
    exception: Any = None


@dataclass(slots=True)
class FakeHostResult:
    """Per-host result wrapper so _result_to_dict can access .failed and [0]."""

    tasks: list[FakeTaskResult]

    @property
    def failed(self) -> bool:
        return any(t.failed for t in self.tasks)

    def __getitem__(self, idx: int) -> FakeTaskResult:
        return self.tasks[idx]


@dataclass(slots=True)
class FakeGlobalState:
    """Stub for Nornir's GlobalState — only the surface tasks.py touches."""

    failed_hosts: set[str] = field(default_factory=set)

    def reset_failed_hosts(self) -> None:
        """Mirror GlobalState.reset_failed_hosts."""
        self.failed_hosts = set()


@dataclass(slots=True)
class FakeNornir:
    """Stub for Nornir instance."""

    inventory: FakeInventory
    data: FakeGlobalState = field(default_factory=FakeGlobalState)

    def filter(
        self,
        name: str | None = None,
        filter_func: Any = None,
        platform: str | None = None,
    ) -> FakeNornir:
        """Filter hosts by name, filter_func, or platform."""
        filtered = dict(self.inventory.hosts._hosts)

        if filter_func is not None:
            filtered = {k: v for k, v in filtered.items() if filter_func(v)}

        if name is not None:
            host = filtered.get(name)
            filtered = {name: host} if host else {}

        if platform is not None:
            filtered = {k: v for k, v in filtered.items() if v.platform == platform}

        return FakeNornir(FakeInventory(FakeHosts(filtered)))

    def run(self, task: Any, **kwargs: Any) -> dict[str, FakeHostResult]:
        """Run a task against all hosts in the filtered inventory."""
        hosts = self.inventory.hosts._hosts
        if not hosts:
            return {}

        # Dispatch based on which kwargs are present
        if "getters" in kwargs:
            getters: list[str] = kwargs["getters"]
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
            return {name: FakeHostResult([FakeTaskResult(result)]) for name in hosts}

        if "commands" in kwargs:
            commands: list[str] = kwargs["commands"]
            result = {cmd: f"Output for: {cmd}" for cmd in commands}
            return {name: FakeHostResult([FakeTaskResult(result)]) for name in hosts}

        # Unrecognized task: fail loudly so new task types must be added explicitly
        raise NotImplementedError(
            f"No dispatch for task={task} with kwargs={set(kwargs.keys())}. "
            "Add a branch in FakeNornir.run()."
        )


def _make_host(name: str, hostname: str, platform: str, groups: list[str]) -> FakeHost:
    return FakeHost(
        name=name,
        hostname=hostname,
        platform=platform,
        groups=[FakeGroup(name=g) for g in groups],
    )


@pytest.fixture(autouse=True)
def _fake_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``NORNIR_CONFIG`` to the sentinel so ``_resolve_config_path``
    returns an existing file and ``_load_config`` returns an empty dict."""
    monkeypatch.setenv("NORNIR_CONFIG", str(_TEST_CONFIG))


@pytest.fixture
def fake_nornir(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeHost]:
    """Patch runner.InitNornir to return a deterministic fake inventory."""
    hosts_data = {
        "spine-01": _make_host("spine-01", "192.168.1.1", "eos", ["spine", "datacenter-a"]),
        "leaf-01": _make_host("leaf-01", "192.168.1.11", "eos", ["leaf", "datacenter-a"]),
    }

    def mock_init(**_: object) -> FakeNornir:
        return FakeNornir(FakeInventory(FakeHosts(hosts_data)))

    monkeypatch.setattr("nornir_napalm_mcp.runner.InitNornir", mock_init)
    return hosts_data
