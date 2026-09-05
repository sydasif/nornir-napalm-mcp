"""NapalmTool — NAPALM-family tools."""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import Context
from nornir_napalm.plugins.tasks import napalm_get

from nornir_mcp.core.envelope import HostOutcome, ToolEnvelope
from nornir_mcp.tools.base.tool import NornirBase
from nornir_mcp.tools.napalm.introspection import list_getters


class NapalmTool(NornirBase):
    """NAPALM-family tools: facts, getter, config retrieval, getter listing."""

    def nornir_get_facts(
        self,
        ctx: Context,
        name: str | list[str] | None = None,
        group: str | None = None,
        platform: str | None = None,
    ) -> ToolEnvelope:
        """Retrieves system facts from network device(s) via NAPALM.

        Fetches device information such as hostname, vendor, model,
        OS version, and serial number.

        Omit all filters to target every device in the inventory.

        Args:
            name: Device name or list of names to query.
            group: Group name to filter devices by.
            platform: Platform name to filter devices by.

        Returns:
            A ToolEnvelope mapping each device name to a HostOutcome. On
            success, ``data`` contains the facts dictionary; on failure,
            ``error`` describes what went wrong. A request-level failure
            (e.g. no devices match the filters) sets the envelope's
            ``error`` to a ``validation`` StructuredError.
        """
        return self._run_task_envelope(
            ctx,
            "nornir_get_facts",
            napalm_get,
            name=name,
            group=group,
            platform=platform,
            getters=["facts"],
        )

    def nornir_run_getter(
        self,
        ctx: Context,
        getter: str,
        name: str | list[str] | None = None,
        group: str | None = None,
        platform: str | None = None,
        getter_options: dict[str, Any] | None = None,
    ) -> ToolEnvelope:
        """Runs any supported NAPALM getter on network device(s).

        Supports all standard NAPALM getters including arp_table, interfaces,
        routes, vlans, and more. Use nornir_list_getters to discover available
        getters for each platform. Use names exactly as returned by
        nornir_list_getters (unprefixed); the server prefixes internally.

        Omit all filters to target every device in the inventory.

        Args:
            getter: NAPALM getter name (e.g., 'arp_table', 'interfaces'),
                unprefixed.
            name: Device name or list of names to query.
            group: Group name to filter devices by.
            platform: Platform name to filter devices by.
            getter_options: Optional getter-specific parameters passed to NAPALM.

        Returns:
            A ToolEnvelope mapping each device name to a HostOutcome containing
            the getter result in ``data`` on success, or ``error`` on failure.
        """
        normalized = getter if getter.startswith("get_") else f"get_{getter}"
        g_opts = {normalized: getter_options} if getter_options is not None else None
        return self._run_task_envelope(
            ctx,
            "nornir_run_getter",
            napalm_get,
            name=name,
            group=group,
            platform=platform,
            getters=[normalized],
            getters_options=g_opts,
        )

    def nornir_get_config(
        self,
        ctx: Context,
        name: str | list[str] | None = None,
        group: str | None = None,
        platform: str | None = None,
        retrieve: Literal["running", "startup", "all"] = "all",
        full: bool = False,
        sanitized: bool = True,
        config_format: Literal["text", "json"] = "text",
    ) -> ToolEnvelope:
        """Retrieves device configuration from network device(s).

        Fetches running and/or startup configuration using NAPALM's config getter.
        By default output is sanitized: raw running configs commonly contain
        password hashes and pre-shared keys, and ``sanitized=True`` strips them
        so credentials are never exposed in MCP responses (spec §22).

        Omit all filters to target every device in the inventory.

        Args:
            name: Device name or list of names to query.
            group: Group name to filter devices by.
            platform: Platform name to filter devices by.
            retrieve: Which config to retrieve — 'running', 'startup', or 'all'.
            full: If True, return the full configuration without filtering.
            sanitized: If True (default), remove sensitive data from the output.
            config_format: Configuration format — 'text' or 'json'.

        Returns:
            A ToolEnvelope mapping each device name to a HostOutcome. On
            success, ``data`` contains the configuration dict with 'running'
            and/or 'startup' keys. On failure, ``error`` describes what went
            wrong.
        """
        getter_options = {
            "config": {
                "retrieve": retrieve,
                "full": full,
                "sanitized": sanitized,
                "format": config_format,
            }
        }
        return self._run_task_envelope(
            ctx,
            "nornir_get_config",
            napalm_get,
            name=name,
            group=group,
            platform=platform,
            getters=["config"],
            getters_options=getter_options,
        )

    def nornir_list_getters(self, ctx: Context) -> ToolEnvelope:
        """Lists available NAPALM getters for each platform in the inventory.

        Introspects the NAPALM driver for each unique platform to discover
        which getters are supported. No device connection is required.

        Returns:
            A ToolEnvelope whose ``results["server"].data`` is a list of
            GetterInfo objects, one per platform, each containing the platform
            name and a sorted list of available getter names. The ``"server"``
            pseudo-host key is used for non-per-host results (see envelope.py).
        """
        return ToolEnvelope(
            operation="nornir_list_getters",
            request_id=self._request_id(ctx),
            results={"server": HostOutcome(success=True, data=list_getters())},
        )
