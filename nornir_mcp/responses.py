"""Response envelope models and output truncation (spec §21, §21.1).

Every tool returns a :class:`ToolEnvelope`:

- ``operation`` — the tool name that produced the response
- ``request_id`` — per-request correlation id (from the MCP Context when
  available, otherwise a ``uuid4().hex`` fallback)
- ``results`` — per-host :class:`HostOutcome` map
- ``error`` — a request-level :class:`StructuredError`, or ``None``

The §21 invariant is exposed as ``ToolEnvelope.success``: top-level success
is True **iff** there is no request-level error AND every host outcome
succeeded. A request-level failure (e.g. no devices matched the filters)
sets ``error`` with an empty ``results`` map.

Non-per-host tools (``nornir_list_inventory``, ``nornir_list_getters``)
wrap their payload under a single pseudo-host key ``"server"`` so every
tool speaks the same envelope. ``nornir_reload_inventory`` returns an
envelope with empty results and request-level success.

Spec §21.1 requires byte-based output truncation with explicit flags:
:func:`maybe_truncate` is the single choke point for that policy, with the
default byte budget coming from ``NORNIR_MCP_MAX_OUTPUT_BYTES`` (fallback
65536).
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict

from nornir_mcp.errors import McpError

# Default byte budget for output truncation (spec §21.1), overridable via
# the NORNIR_MCP_MAX_OUTPUT_BYTES environment variable.
_DEFAULT_MAX_OUTPUT_BYTES = 65536
_ENV_MAX_OUTPUT_BYTES = "NORNIR_MCP_MAX_OUTPUT_BYTES"


class StructuredError(BaseModel):
    """A request- or host-level error payload (spec §22 shape).

    ``type`` is a string from the :class:`nornir_mcp.errors.ErrorType`
    catalog; ``retryable`` follows the spec §23 policy.
    """

    model_config = ConfigDict(frozen=True)

    type: str
    message: str
    host: str | None = None
    operation: str | None = None
    retryable: bool = False


class HostOutcome(BaseModel):
    """Per-host result inside a ToolEnvelope.

    Makes success/failure explicit: on failure ``data`` is ``None`` and
    ``error`` describes what went wrong; on success ``error`` is ``None``.
    """

    model_config = ConfigDict(frozen=True)

    success: bool
    data: Any | None = None
    error: StructuredError | None = None


class ToolEnvelope(BaseModel):
    """Standard response envelope for every tool (spec §21)."""

    operation: str
    request_id: str
    results: dict[str, HostOutcome]
    error: StructuredError | None = None

    @property
    def success(self) -> bool:
        """True iff there is no request-level error and every host succeeded.

        Invariant (spec §21): top-level ``success`` is True exactly when
        ``error`` is ``None`` and all host outcomes report success.
        """
        return self.error is None and all(outcome.success for outcome in self.results.values())


def outcome_from_mcp_error(exc: McpError) -> HostOutcome:
    """Build a failed :class:`HostOutcome` from a categorized :class:`McpError`.

    Args:
        exc: The categorized error to convert.

    Returns:
        A ``HostOutcome`` with ``success=False`` whose ``error`` mirrors
        the error's §22 fields (type, message, host, operation, retryable).
    """
    return HostOutcome(
        success=False,
        data=None,
        error=StructuredError(
            type=exc.error_type.value,
            message=exc.message,
            host=exc.host,
            operation=exc.operation,
            retryable=exc.retryable,
        ),
    )


def maybe_truncate(text: str, max_bytes: int | None = None) -> tuple[str, bool, int]:
    """Truncate *text* to at most *max_bytes* UTF-8 bytes (spec §21.1).

    Truncation is UTF-8 safe: a multi-byte character split by the byte
    budget is dropped whole rather than corrupted.

    Args:
        text: The text to truncate.
        max_bytes: Byte budget. If None, the default from the
            ``NORNIR_MCP_MAX_OUTPUT_BYTES`` environment variable is used,
            falling back to 65536 when unset or invalid.

    Returns:
        A tuple of ``(possibly_truncated_text, truncated_flag,
        original_byte_size)``. ``truncated_flag`` is True iff the text was
        shortened; ``original_byte_size`` is the UTF-8 byte length of the
        input before any truncation.
    """
    if max_bytes is None:
        max_bytes = _default_max_bytes()

    encoded = text.encode("utf-8")
    original_size = len(encoded)
    if original_size <= max_bytes:
        return text, False, original_size

    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, True, original_size


def _default_max_bytes() -> int:
    """Resolve the default truncation budget from the environment."""
    raw = os.environ.get(_ENV_MAX_OUTPUT_BYTES)
    if raw is None:
        return _DEFAULT_MAX_OUTPUT_BYTES
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_MAX_OUTPUT_BYTES
