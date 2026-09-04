"""Tests for the global execution lock (D7 concurrency hardening).

FastMCP runs sync tools in a threadpool while ``get_nornir()`` is a
process-wide singleton, so overlapping calls can race on Nornir's
GlobalState. ``execution_lock()`` serializes every device-touching run;
these tests pin that the lock is held during execution, is reentrant, and
is always released.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from nornir_mcp import runner
from nornir_mcp.runner import EXECUTION_LOCK, execution_lock
from nornir_mcp.tasks import run_nornir_task
from tests.conftest import FakeTaskResult, fake_netmiko_send_command


@pytest.fixture(autouse=True)
def _fake_env(request: pytest.FixtureRequest) -> Iterator[None]:
    """Patch InitNornir and reset the singleton around each test."""
    request.getfixturevalue("fake_nornir")
    runner.reset_nornir()
    yield
    runner.reset_nornir()


def test_lock_is_held_during_task_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task running inside FakeNornir observes the lock already held.

    The probe must come from a *different* thread: ``RLock`` is owned by a
    single thread, so a re-acquire from the task's own thread would succeed
    even while the lock is held (reentrancy). A worker thread attempting a
    non-blocking acquire only fails when the lock is genuinely held.
    """
    probed: list[bool] = []

    def fake_netmiko_send_command_probe(
        task: object, command_string: str = "", **kw: object
    ) -> FakeTaskResult:
        result: list[bool] = []

        def try_acquire_from_other_thread() -> None:
            acquired = EXECUTION_LOCK.acquire(blocking=False)
            if acquired:
                EXECUTION_LOCK.release()
            result.append(not acquired)

        worker = threading.Thread(target=try_acquire_from_other_thread)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive(), "lock probe thread hung"
        probed.extend(result)
        return FakeTaskResult(result="probe-ok")

    run_nornir_task(
        fake_netmiko_send_command_probe,
        operation="test",
        name="spine-01",
        command_string="show version",
    )
    assert probed == [True]


def test_lock_is_reentrant() -> None:
    """run_nornir_task may be called while already holding the lock (RLock)."""
    result: list[str] = []

    def worker() -> None:
        with execution_lock():
            run_nornir_task(
                fake_netmiko_send_command,
                operation="test",
                name="spine-01",
                command_string="show version",
            )
            result.append("done")

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "deadlock: run_nornir_task inside execution_lock hung"
    assert result == ["done"]


def test_lock_released_after_task() -> None:
    """After a run returns, the lock is free for a fresh acquisition."""
    run_nornir_task(
        fake_netmiko_send_command,
        operation="test",
        name="spine-01",
        command_string="show version",
    )
    assert EXECUTION_LOCK.acquire(blocking=False)
    EXECUTION_LOCK.release()
