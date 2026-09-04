"""Tests for capability.py and the conftest fake netmiko infrastructure.

The netmiko fakes are load-bearing test infrastructure for the upcoming
CLI tools, so they get their own tests: canned output shapes and the
invocation record that later "nothing was executed" assertions rely on.
"""

from __future__ import annotations

from typing import Any

import pytest

from nornir_mcp.capability import (
    NETMIKO_DEVICE_TYPES,
    netmiko_device_type,
    supports_cli,
)
from nornir_mcp.errors import UnsupportedOperationError
from tests.conftest import (
    FakeHost,
    FakeHosts,
    FakeInventory,
    FakeNornir,
    NetmikoTaskShim,
    fake_netmiko_save_config,
    fake_netmiko_send_command,
    fake_netmiko_send_config,
)


def _host(name: str, platform: str = "ios") -> FakeHost:
    return FakeHost(name=name, hostname="10.0.0.1", platform=platform, groups=[])


# ---------------------------------------------------------------------------
# capability.py — platform -> netmiko device_type gate
# ---------------------------------------------------------------------------


def test_netmiko_device_type_known_platforms() -> None:
    """Known platforms map to their netmiko device_type."""
    assert netmiko_device_type("ios") == "cisco_ios"
    assert netmiko_device_type("eos") == "arista_eos"
    assert NETMIKO_DEVICE_TYPES == {"ios": "cisco_ios", "eos": "arista_eos"}
    assert supports_cli("ios") is True
    assert supports_cli("eos") is True


def test_netmiko_device_type_unknown_platform_raises_unsupported_operation() -> None:
    """Unknown platforms raise a capability error listing what is supported."""
    assert supports_cli("junos") is False
    with pytest.raises(UnsupportedOperationError) as excinfo:
        netmiko_device_type("junos")
    message = str(excinfo.value)
    assert "junos" in message
    assert "ios" in message  # lists supported platforms
    assert "eos" in message


# ---------------------------------------------------------------------------
# conftest netmiko fakes
# ---------------------------------------------------------------------------


def test_fake_netmiko_send_command_returns_canned_output(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """send_command returns a canned, per-host output string."""
    spine = _host("spine-01", platform="ios")
    result = fake_netmiko_send_command(NetmikoTaskShim(spine), command_string="show version")
    assert result.result == "canned output [spine-01]: show version"

    # Output is host-distinct so multi-host runs are assertable.
    leaf = _host("leaf-01")
    other = fake_netmiko_send_command(NetmikoTaskShim(leaf), command_string="show version")
    assert other.result == "canned output [leaf-01]: show version"

    assert len(netmiko_fakes) == 2
    assert netmiko_fakes[0] == {
        "function": "netmiko_send_command",
        "host": "spine-01",
        "command_string": "show version",
        "kwargs": {},
    }


def test_fake_netmiko_send_config_records_invocation(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """send_config records exactly what it was asked to execute."""
    spine = _host("spine-01")
    config_lines = ["hostname spine-01", "interface Ethernet1"]
    result = fake_netmiko_send_config(NetmikoTaskShim(spine), config_commands=config_lines)
    assert result.changed is True
    assert result.result == (
        "canned config [spine-01]: hostname spine-01\n"
        "canned config [spine-01]: interface Ethernet1"
    )

    assert len(netmiko_fakes) == 1
    call = netmiko_fakes[0]
    assert call["function"] == "netmiko_send_config"
    assert call["host"] == "spine-01"
    assert call["config_commands"] == config_lines
    assert call["config_file"] is None
    assert call["dry_run"] is None


def test_fake_netmiko_send_config_rejects_dry_run(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """dry_run is unsupported by send_config (mirrors the real plugin)."""
    spine = _host("spine-01")
    result = fake_netmiko_send_config(
        NetmikoTaskShim(spine), config_commands=["hostname x"], dry_run=True
    )
    assert result.failed is True
    assert result.result == "netmiko_send_config does not support dry_run"


def test_fake_netmiko_save_config_records_invocation(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """save_config records so tests can assert nothing was executed."""
    spine = _host("spine-01")
    result = fake_netmiko_save_config(NetmikoTaskShim(spine))
    assert result.changed is True
    assert netmiko_fakes == [
        {
            "function": "netmiko_save_config",
            "host": "spine-01",
            "cmd": "",
            "confirm": False,
            "kwargs": {},
        }
    ]


def test_fake_netmiko_tasks_dispatch_per_host_through_fake_nornir(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """FakeNornir.run invokes fake netmiko tasks once per host."""
    hosts = {
        "spine-01": _host("spine-01"),
        "leaf-01": _host("leaf-01"),
    }
    nr = FakeNornir(FakeInventory(FakeHosts(hosts)))
    results = nr.run(fake_netmiko_send_command, command_string="show version")

    assert set(results) == {"spine-01", "leaf-01"}
    assert results["spine-01"][0].result == "canned output [spine-01]: show version"
    assert results["leaf-01"][0].result == "canned output [leaf-01]: show version"
    assert [call["host"] for call in netmiko_fakes] == ["spine-01", "leaf-01"]
