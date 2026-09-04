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

    result: Any
    failed: bool = False
    exception: Any = None
    diff: str = ""
    changed: bool = False


@dataclass(slots=True)
class FakeHostResult:
    """Per-host result wrapper so _results_to_outcomes can access .failed and [0]."""

    tasks: list[FakeTaskResult]

    @property
    def failed(self) -> bool:
        return any(t.failed for t in self.tasks)

    def __len__(self) -> int:
        """Mirror MultiResult's list-like truthiness (empty means no tasks)."""
        return len(self.tasks)

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

        task_name = getattr(task, "__name__", "")

        # Netmiko fakes (monkeypatched CLI tasks): invoke the fake once per
        # host so canned output and the invocation record flow through the
        # same path the real CLI tools use.
        if task_name.startswith("fake_netmiko_"):
            return {
                host.name: FakeHostResult([task(NetmikoTaskShim(host), **kwargs)])
                for host in hosts.values()
            }

        # Config / change tasks: return a diff-bearing payload.
        # Mirror nornir-napalm semantics for the flags the tools surface:
        #   * napalm_configure sets changed = (diff is non-empty) *before* the
        #     dry_run commit/discard check, so changed is independent of dry_run.
        #   * napalm_rollback / napalm_confirm_commit set changed only when the
        #     action is actually taken (dry_run=False).
        if task_name in ("napalm_configure", "napalm_rollback", "napalm_confirm_commit"):
            dry_run = kwargs.get("dry_run", True)
            result_str = (
                "Rollback completed"
                if task_name == "napalm_rollback" and not dry_run
                else "Commit confirm completed"
                if task_name == "napalm_confirm_commit" and not dry_run
                else ""
            )
            changed = bool(result_str) or (task_name == "napalm_configure")
            payload = FakeTaskResult(
                result=result_str,
                diff="--- a\n+++ b\n+hostname foo\n",
                changed=changed,
            )
            return {name: FakeHostResult([payload]) for name in hosts}

        # Dispatch based on which kwargs are present.
        # Mirror nornir-napalm napalm_get semantics: the result is keyed by
        # the getter name as passed, while the device method is looked up
        # under its normalized get_-prefixed form.
        if "getters" in kwargs:
            getters: list[str] = kwargs["getters"]
            # NAPALM's config getter sanitizes only when explicitly asked
            # (the nornir_get_config tool passes sanitized=True by default);
            # capture/backup paths keep the raw config verbatim.
            getters_options = kwargs.get("getters_options") or {}
            config_options = (
                getters_options.get("config") or {} if isinstance(getters_options, dict) else {}
            )
            sanitized = bool(config_options.get("sanitized", False))
            payloads = {
                "get_facts": {
                    "hostname": "test-host",
                    "vendor": "Arista",
                    "model": "7280R",
                },
                "get_interfaces": {"Ethernet1": {"state": "up", "speed": "1000"}},
                "get_interfaces_ip": {"Ethernet1": {"ipv4": {"10.0.0.1/24": {}}}},
                "get_config": {
                    "running": _strip_secrets(_SECRET_RUNNING) if sanitized else _SECRET_RUNNING,
                    "startup": _strip_secrets(_SECRET_STARTUP) if sanitized else _SECRET_STARTUP,
                },
            }
            result = {
                g: payloads.get(g if g.startswith("get_") else f"get_{g}", {"ok": True})
                for g in getters
            }
            return {name: FakeHostResult([FakeTaskResult(result)]) for name in hosts}

        if "commands" in kwargs:
            commands: list[str] = kwargs["commands"]
            result = {cmd: f"Output for: {cmd}" for cmd in commands}
            return {name: FakeHostResult([FakeTaskResult(result)]) for name in hosts}

        if "getters" in kwargs or "dest" in kwargs:
            # napalm_ping and similar tasks return ping/compliance data
            return {
                name: FakeHostResult([FakeTaskResult(result={"success": True})]) for name in hosts
            }

        if "src" in kwargs or "validation_source" in kwargs:
            # napalm_validate returns compliance dict with result/complies keys
            return {
                name: FakeHostResult([FakeTaskResult(result={"result": {}, "complies": True})])
                for name in hosts
            }

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


# ---------------------------------------------------------------------------
# Fake netmiko tasks (CLI tooling)
#
# The CLI tools import nornir-netmiko task functions at module level in
# server.py; the `netmiko_fakes` fixture replaces those names with these
# canned implementations. Every invocation is appended to `_netmiko_calls`
# so tests can assert what was — and was not — executed. FakeNornir.run
# invokes any task whose name starts with ``fake_netmiko_`` once per host.
# ---------------------------------------------------------------------------

_netmiko_calls: list[dict[str, Any]] = []

# Per-host transcript overrides for fake_netmiko_send_config, keyed by host
# name. Tests set these to exercise transcript parsing (clean vs error
# variants) without touching a device. Cleared by the `netmiko_fakes`
# fixture alongside the invocation record.
netmiko_config_transcripts: dict[str, str] = {}


class NetmikoTaskShim:
    """Minimal stand-in for a Nornir Task: exposes ``.host`` to fake tasks."""

    def __init__(self, host: FakeHost) -> None:
        self.host = host


def fake_netmiko_send_command(
    task: NetmikoTaskShim, command_string: str = "", **kwargs: Any
) -> FakeTaskResult:
    """Canned stand-in for netmiko_send_command.

    Mirrors the real plugin's result shape: a plain per-host output string.
    """
    host = task.host
    _netmiko_calls.append(
        {
            "function": "netmiko_send_command",
            "host": host.name,
            "command_string": command_string,
            "kwargs": kwargs,
        }
    )
    return FakeTaskResult(result=f"canned output [{host.name}]: {command_string}")


def fake_netmiko_send_commands(
    task: NetmikoTaskShim, commands: list[str] | None = None, **kwargs: Any
) -> FakeTaskResult:
    """Canned stand-in for a bulk command runner.

    Mirrors the batch task's result shape: a per-command output map.
    """
    host = task.host
    commands = commands or []
    _netmiko_calls.append(
        {
            "function": "netmiko_send_commands",
            "host": host.name,
            "commands": commands,
            "kwargs": kwargs,
        }
    )
    output = {cmd: f"canned output [{host.name}]: {cmd}" for cmd in commands}
    return FakeTaskResult(result=output)


def fake_netmiko_send_config(
    task: NetmikoTaskShim,
    config_commands: list[str] | None = None,
    config_file: str | None = None,
    dry_run: bool | None = None,
    **kwargs: Any,
) -> FakeTaskResult:
    """Canned stand-in for netmiko_send_config.

    Mirrors the real plugin's constraints: ``dry_run`` is unsupported and a
    missing config source is an error — both surface as failed hosts.
    """
    host = task.host
    _netmiko_calls.append(
        {
            "function": "netmiko_send_config",
            "host": host.name,
            "config_commands": config_commands,
            "config_file": config_file,
            "dry_run": dry_run,
            "kwargs": kwargs,
        }
    )
    if dry_run is True:
        return FakeTaskResult(result="netmiko_send_config does not support dry_run", failed=True)
    if config_commands is None and config_file is None:
        return FakeTaskResult(
            result="Must specify either config_commands or config_file", failed=True
        )
    lines = config_commands if config_commands is not None else ["<config from file>"]
    canned = "\n".join(f"canned config [{host.name}]: {line}" for line in lines)
    output = netmiko_config_transcripts.get(host.name, canned)
    return FakeTaskResult(result=output, changed=True)


def fake_netmiko_save_config(
    task: NetmikoTaskShim, cmd: str = "", confirm: bool = False, **kwargs: Any
) -> FakeTaskResult:
    """Canned stand-in for netmiko_save_config.

    Returns a successful changed result and records the invocation so
    tests can assert a save was (or was never) executed.
    """
    host = task.host
    _netmiko_calls.append(
        {
            "function": "netmiko_save_config",
            "host": host.name,
            "cmd": cmd,
            "confirm": confirm,
            "kwargs": kwargs,
        }
    )
    return FakeTaskResult(result=f"canned save [{host.name}]: configuration saved", changed=True)


@pytest.fixture
def netmiko_fakes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Monkeypatch fake netmiko tasks into the netmiko tool namespace.

    Replaces the names ``nornir_mcp.tools.netmiko.tool`` imports from
    nornir-netmiko at module level (``netmiko_send_command`` etc.) with the
    canned fakes above and resets the shared invocation record. Yields the
    record list so tests can assert what was (or was never) executed.

    Object-form monkeypatching is used so the fixture also works before
    the module imports these names (attribute-creation, not getattr-check).
    """
    import nornir_mcp.tools.netmiko.tool as netmiko_tool_module

    _netmiko_calls.clear()
    netmiko_config_transcripts.clear()
    monkeypatch.setattr(
        netmiko_tool_module, "netmiko_send_command", fake_netmiko_send_command, raising=False
    )
    monkeypatch.setattr(
        netmiko_tool_module, "netmiko_send_commands", fake_netmiko_send_commands, raising=False
    )
    monkeypatch.setattr(
        netmiko_tool_module, "netmiko_send_config", fake_netmiko_send_config, raising=False
    )
    monkeypatch.setattr(
        netmiko_tool_module, "netmiko_save_config", fake_netmiko_save_config, raising=False
    )
    return _netmiko_calls


def _strip_secrets(text: str) -> str:
    """Simulate NAPALM ``sanitized=True``: drop lines carrying secrets."""
    return "\n".join(
        line
        for line in text.splitlines()
        if not any(keyword in line.lower() for keyword in ("password", "secret"))
    )


# Fake device configs deliberately contain a password-like string so the
# no-secrets e2e test is genuine: sanitized responses must strip it, while
# backups retain the raw config verbatim by design (spec §8).
_SECRET_RUNNING = "! running-config\nhostname test-host\nenable secret 5 $1$e2e$f00\n"
_SECRET_STARTUP = "! startup-config\nhostname test-host\nenable secret 5 $1$e2e$f00\n"


@pytest.fixture
def anyio_backend() -> str:
    """Run ``@pytest.mark.anyio`` tests on the asyncio backend."""
    return "asyncio"


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

    monkeypatch.setattr("nornir_mcp.core.runner.InitNornir", mock_init)
    return hosts_data
