"""CoreBase — shared envelope and selection plumbing for every tool class.

``CoreBase`` is the root of the four-class chain
(``CoreBase → NornirBase → (NapalmTool | NetmikoTool)``). It owns the
request-plumbing every tool uses — correlation id, the run-and-envelope
wrapper, the request-level validation envelope, and device selection — as
**private** methods and registers no tools itself.

Stateless services (the runner cache, lock, backup store, audit logger, and
policy/capability helpers) are process-wide singletons and live at the module
level in sibling ``core`` modules; ``CoreBase`` imports them like any core
consumer and never stores them on ``self``. An instance of any tool class is
a pure grouping object — owning that state on an instance would let a second
instance silently bypass the global lock/cache (spec D7).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastmcp import Context

from nornir_mcp.core.envelope import (
    StructuredError,
    ToolEnvelope,
)
from nornir_mcp.core.errors import McpError, ValidationError
from nornir_mcp.core.runner import get_nornir
from nornir_mcp.core.tasks import _filter_devices, run_nornir_task


class CoreBase:
    """Shared envelope/selection plumbing for all tool classes.

    Inherits nothing; every tool class inherits from this. No tools are
    registered here — only the private helpers they delegate to.
    """

    def _request_id(self, ctx: Context | None) -> str:
        """Correlation id for the current request.

        Prefers ``ctx.request_id`` from the injected MCP Context. FastMCP
        raises ``RuntimeError`` when no MCP session is established (e.g. a
        direct invocation), which is treated as "not available"; in that case a
        fresh ``uuid4().hex`` is used.
        """
        try:
            request_id: str | None = ctx.request_id if ctx is not None else None
        except (RuntimeError, AttributeError):
            request_id = None
        return request_id if request_id is not None else uuid.uuid4().hex

    def _run_task_envelope(
        self,
        ctx: Context,
        operation: str,
        task: Any,
        *,
        name: str | list[str] | None = None,
        group: str | None = None,
        platform: str | None = None,
        **task_kwargs: Any,
    ) -> ToolEnvelope:
        """Run a Nornir task and wrap the per-host outcomes in a ToolEnvelope.

        Request-level failures — ``ValidationError`` from ``_filter_devices``
        (e.g. an explicitly empty name list) or the bare ``ValueError`` for no
        matching devices — become a request-level StructuredError on the
        envelope instead of a raised exception.
        """
        try:
            outcomes = run_nornir_task(
                task, operation=operation, name=name, group=group, platform=platform, **task_kwargs
            )
        except McpError as exc:
            return ToolEnvelope(
                operation=operation,
                request_id=self._request_id(ctx),
                results={},
                error=StructuredError(
                    type=exc.error_type.value,
                    message=exc.message,
                    host=exc.host,
                    operation=operation,
                    retryable=exc.retryable,
                ),
            )
        except ValueError as exc:
            return ToolEnvelope(
                operation=operation,
                request_id=self._request_id(ctx),
                results={},
                error=StructuredError(
                    type="validation",
                    message=str(exc),
                    operation=operation,
                    retryable=False,
                ),
            )
        return ToolEnvelope(
            operation=operation, request_id=self._request_id(ctx), results=outcomes
        )

    def _validation_envelope(self, operation: str, request_id: str, message: str) -> ToolEnvelope:
        """A request-level validation failure envelope (empty results)."""
        return ToolEnvelope(
            operation=operation,
            request_id=request_id,
            results={},
            error=StructuredError(
                type="validation",
                message=message,
                operation=operation,
                retryable=False,
            ),
        )

    def _select_targets(
        self, operation: str, request_id: str, name: Any, group: str | None, platform: str | None
    ) -> tuple[ToolEnvelope | None, Any | None]:
        """Request-level device selection.

        Returns ``(None, targets)`` on success, or ``(error_envelope, None)``
        when no devices match / the selection is explicitly empty.
        """
        try:
            nr = get_nornir()
            nr.data.reset_failed_hosts()
            targets = _filter_devices(nr, name=name, group=group, platform=platform)
        except (ValidationError, ValueError) as exc:
            message = exc.message if isinstance(exc, ValidationError) else str(exc)
            return self._validation_envelope(operation, request_id, message), None
        return None, targets
