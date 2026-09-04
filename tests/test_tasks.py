"""Tests for tasks.py — device filtering and task execution helpers."""

from __future__ import annotations

import pytest

from nornir_napalm_mcp.models import HostResult
from nornir_napalm_mcp.tasks import _filter_devices, _result_to_dict
from tests.conftest import (
    FakeGroup,
    FakeHost,
    FakeHostResult,
    FakeHosts,
    FakeInventory,
    FakeNornir,
    FakeTaskResult,
)


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


# ---------------------------------------------------------------------------
# _result_to_dict
# ---------------------------------------------------------------------------


def test_result_to_dict_success() -> None:
    """Verify _result_to_dict maps successful results to HostResult(ok=True)."""
    result = {"spine-01": FakeHostResult([FakeTaskResult(result={"facts": {}})])}
    out = _result_to_dict(result)  # type: ignore[arg-type]
    assert out == {"spine-01": HostResult(ok=True, data={"facts": {}})}


def test_result_to_dict_failed_with_exception() -> None:
    """Verify _result_to_dict surfaces the exception message on failure."""
    result = {
        "leaf-01": FakeHostResult(
            [FakeTaskResult(result={}, failed=True, exception=RuntimeError("boom"))]
        )
    }
    out = _result_to_dict(result)  # type: ignore[arg-type]
    assert out == {"leaf-01": HostResult(ok=False, error="boom")}


def test_result_to_dict_failed_without_exception() -> None:
    """Verify _result_to_dict falls back to the result string on failure."""
    result = {"leaf-01": FakeHostResult([FakeTaskResult(result="bad output", failed=True)])}
    out = _result_to_dict(result)  # type: ignore[arg-type]
    assert out == {"leaf-01": HostResult(ok=False, error="bad output")}


def test_result_to_dict_empty_tasks() -> None:
    """Verify _result_to_dict flags hosts with no tasks returned."""
    result = {"leaf-01": FakeHostResult([])}
    out = _result_to_dict(result)  # type: ignore[arg-type]
    assert out == {"leaf-01": HostResult(ok=False, error="No tasks returned for host")}


def test_result_to_dict_config_style_with_diff() -> None:
    """Verify _result_to_dict extracts diff/changed for config-style Results."""
    result = {
        "spine-01": FakeHostResult(
            [FakeTaskResult(result="", diff="--- a\n+++ b\n+hostname foo\n", changed=True)]
        )
    }
    out = _result_to_dict(result, config_style=True)  # type: ignore[arg-type]
    assert out == {
        "spine-01": HostResult(
            ok=True,
            data={"diff": "--- a\n+++ b\n+hostname foo\n", "changed": True, "result": ""},
        )
    }


def test_result_to_dict_config_style_no_diff() -> None:
    """Verify config-style with empty diff still yields a clean data dict."""
    result = {"spine-01": FakeHostResult([FakeTaskResult(result="Rollback completed")])}
    out = _result_to_dict(result, config_style=True)  # type: ignore[arg-type]
    assert out["spine-01"].data == {"diff": "", "changed": False, "result": "Rollback completed"}
