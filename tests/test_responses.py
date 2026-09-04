"""Tests for responses.py — §21 envelope invariant and §21.1 truncation."""

from __future__ import annotations

import pytest

from nornir_mcp.errors import DeviceConnectionError
from nornir_mcp.responses import (
    HostOutcome,
    StructuredError,
    ToolEnvelope,
    maybe_truncate,
    outcome_from_mcp_error,
)


def _envelope_with(outcomes: dict[str, HostOutcome]) -> ToolEnvelope:
    return ToolEnvelope(
        operation="nornir_get_facts",
        request_id="req-1",
        results=outcomes,
    )


def _ok(host: str = "spine-01") -> HostOutcome:
    return HostOutcome(success=True, data={host: "ok"})


def _fail(host: str = "leaf-01") -> HostOutcome:
    return HostOutcome(
        success=False,
        data=None,
        error=StructuredError(
            type="connection",
            message="boom",
            host=host,
            operation="nornir_get_facts",
            retryable=True,
        ),
    )


# ---------------------------------------------------------------------------
# ToolEnvelope.success — §21 iff invariant
# ---------------------------------------------------------------------------


def test_envelope_success_iff_all_hosts_succeed() -> None:
    """success is True iff no request-level error and every host succeeded."""
    # All hosts succeeded, no request-level error -> True.
    assert _envelope_with({"spine-01": _ok(), "leaf-01": _ok()}).success is True
    # A single failed host flips the envelope to failure (even though the
    # other host succeeded and there is no request-level error).
    assert _envelope_with({"spine-01": _ok(), "leaf-01": _fail()}).success is False
    # Empty results with no error: vacuously all hosts succeeded -> True.
    assert _envelope_with({}).success is True


def test_envelope_request_level_error_makes_success_false() -> None:
    """A request-level error fails the envelope even when hosts succeeded."""
    env = ToolEnvelope(
        operation="nornir_get_facts",
        request_id="req-1",
        results={"spine-01": _ok()},
        error=StructuredError(
            type="validation",
            message="No devices match the provided filters",
            operation="nornir_get_facts",
        ),
    )
    assert env.success is False
    assert env.error is not None


# ---------------------------------------------------------------------------
# outcome_from_mcp_error
# ---------------------------------------------------------------------------


def test_host_failure_produces_structured_error_with_retryable_true() -> None:
    """McpError maps to a failed HostOutcome carrying the §22 fields."""
    exc = DeviceConnectionError(
        "SSH connection timed out",
        host="spine-01",
        operation="nornir_get_facts",
    )
    outcome = outcome_from_mcp_error(exc)
    assert outcome.success is False
    assert outcome.data is None
    assert outcome.error is not None
    assert outcome.error.type == "connection"
    assert outcome.error.message == "SSH connection timed out"
    assert outcome.error.host == "spine-01"
    assert outcome.error.operation == "nornir_get_facts"
    assert outcome.error.retryable is True


# ---------------------------------------------------------------------------
# maybe_truncate — §21.1 byte-based truncation
# ---------------------------------------------------------------------------


def test_truncate_flags_original_size_and_sets_truncated() -> None:
    """Over-limit text is truncated, flagged, and reports original size."""
    text = "x" * 200
    truncated, flagged, original_size = maybe_truncate(text, max_bytes=100)
    assert flagged is True
    assert original_size == 200
    assert len(truncated.encode("utf-8")) == 100


def test_truncate_noop_under_limit() -> None:
    """Under-limit text passes through untouched with truncated=False."""
    text = "hello world"
    truncated, flagged, original_size = maybe_truncate(text, max_bytes=100)
    assert truncated == text
    assert flagged is False
    assert original_size == len(text.encode("utf-8"))


def test_truncate_is_utf8_safe() -> None:
    """Truncation never splits a multi-byte character."""
    text = "héllo wörld " * 50  # é and ö are 2 bytes each in UTF-8
    truncated, flagged, original_size = maybe_truncate(text, max_bytes=64)
    assert flagged is True
    assert original_size == len(text.encode("utf-8"))
    assert len(truncated.encode("utf-8")) <= 64
    # Must decode cleanly — no replacement chars from a split codepoint.
    truncated.encode("utf-8").decode("utf-8")


def test_truncate_default_limit_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default limit honors NORNIR_MCP_MAX_OUTPUT_BYTES."""
    monkeypatch.setenv("NORNIR_MCP_MAX_OUTPUT_BYTES", "16")
    text = "x" * 100
    truncated, flagged, original_size = maybe_truncate(text)
    assert flagged is True
    assert original_size == 100
    assert len(truncated.encode("utf-8")) == 16


def test_truncate_default_limit_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the env var, the default limit is 65536 bytes."""
    monkeypatch.delenv("NORNIR_MCP_MAX_OUTPUT_BYTES", raising=False)
    text = "x" * (65536 + 10)
    truncated, flagged, original_size = maybe_truncate(text)
    assert flagged is True
    assert original_size == 65536 + 10
    assert len(truncated.encode("utf-8")) == 65536
