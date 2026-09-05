"""Tests for tasks.py — device filtering and task execution helpers."""

from __future__ import annotations

import pytest

from nornir_mcp.core.envelope import HostOutcome
from nornir_mcp.core.errors import ValidationError
from nornir_mcp.core.tasks import _filter_devices, _results_to_outcomes
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


def test_empty_name_list_raises_validation_error() -> None:
    """An explicitly empty name selection is an error, not 'all devices'."""
    nr = FakeNornir(FakeInventory(FakeHosts({})))
    with pytest.raises(ValidationError, match="explicitly empty device list provided"):
        _filter_devices(nr, name=[])
    with pytest.raises(ValidationError, match="explicitly empty device list provided"):
        _filter_devices(nr, name="")


def test_no_filters_targets_all_devices() -> None:
    """Omitted filters target the entire inventory."""
    hosts = {
        "a": FakeHost(name="a", hostname="10.0.0.1", platform="eos", groups=[]),
        "b": FakeHost(name="b", hostname="10.0.0.2", platform="eos", groups=[]),
    }
    nr = FakeNornir(FakeInventory(FakeHosts(hosts)))
    filtered = _filter_devices(nr)
    assert set(filtered.inventory.hosts._hosts.keys()) == {"a", "b"}


# ---------------------------------------------------------------------------
# _results_to_outcomes
# ---------------------------------------------------------------------------


def test_results_to_outcomes_success() -> None:
    """Verify successful hosts map to a success outcome with the raw data."""
    result = {"spine-01": FakeHostResult([FakeTaskResult(result={"facts": {}})])}
    out = _results_to_outcomes(result, "nornir_get_facts")  # type: ignore[arg-type]
    assert out == {"spine-01": HostOutcome(success=True, data={"facts": {}})}


def test_results_to_outcomes_failed_with_exception() -> None:
    """Verify exceptions become a retryable 'connection' structured error."""
    result = {
        "leaf-01": FakeHostResult(
            [FakeTaskResult(result={}, failed=True, exception=RuntimeError("boom"))]
        )
    }
    out = _results_to_outcomes(result, "nornir_run_getter")  # type: ignore[arg-type]
    outcome = out["leaf-01"]
    assert outcome.success is False
    assert outcome.data is None
    assert outcome.error is not None
    assert outcome.error.type == "connection"
    assert outcome.error.retryable is True
    assert outcome.error.host == "leaf-01"
    assert outcome.error.operation == "nornir_run_getter"
    assert outcome.error.message == "boom"


def test_results_to_outcomes_failed_without_exception() -> None:
    """Verify result-string failures become a non-retryable internal error."""
    result = {"leaf-01": FakeHostResult([FakeTaskResult(result="bad output", failed=True)])}
    out = _results_to_outcomes(result, "nornir_run_getter")  # type: ignore[arg-type]
    outcome = out["leaf-01"]
    assert outcome.success is False
    assert outcome.error is not None
    assert outcome.error.type == "internal"
    assert outcome.error.retryable is False
    assert outcome.error.message == "bad output"


def test_results_to_outcomes_empty_tasks() -> None:
    """Verify hosts with no tasks returned are flagged as failures."""
    result = {"leaf-01": FakeHostResult([])}
    out = _results_to_outcomes(result, "nornir_get_facts")  # type: ignore[arg-type]
    outcome = out["leaf-01"]
    assert outcome.success is False
    assert outcome.error is not None
    assert outcome.error.message == "No tasks returned for host"
