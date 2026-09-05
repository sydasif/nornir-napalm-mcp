"""NornirBase — engine-agnostic server tools."""

from __future__ import annotations

from fastmcp import Context
from pydantic import BaseModel, ConfigDict

from nornir_mcp.core.audit import get_audit_logger
from nornir_mcp.core.base import CoreBase
from nornir_mcp.core.envelope import HostOutcome, ToolEnvelope, outcome_from_mcp_error
from nornir_mcp.core.errors import InternalError, McpError, ValidationError
from nornir_mcp.core.runner import get_nornir, reset_nornir
from nornir_mcp.core.storage import get_backup_store
from nornir_mcp.tools.base.capture import capture_running_config


class InventoryDevice(BaseModel):
    """A network device in the Nornir inventory."""

    model_config = ConfigDict(frozen=True)

    name: str
    hostname: str
    platform: str
    groups: list[str]


class NornirBase(CoreBase):
    """Engine-agnostic server tools: inventory, reload, backup capture/list."""

    def nornir_list_inventory(self, ctx: Context) -> ToolEnvelope:
        """Lists all devices in the Nornir inventory.

        Returns:
            A ToolEnvelope whose ``results["server"].data`` is a sorted list of
            InventoryDevice objects, each containing the device name, hostname,
            platform, and group membership. The ``"server"`` pseudo-host key is
            used for non-per-host results (see envelope.py).
        """
        nr = get_nornir()
        devices = [
            InventoryDevice(
                name=host.name,
                hostname=str(host.hostname),
                platform=str(host.platform),
                groups=[g.name for g in host.groups],
            )
            for host in sorted(nr.inventory.hosts.values(), key=lambda h: h.name)
        ]
        return ToolEnvelope(
            operation="nornir_list_inventory",
            request_id=self._request_id(ctx),
            results={"server": HostOutcome(success=True, data=devices)},
        )

    def nornir_reload_inventory(self, ctx: Context) -> ToolEnvelope:
        """Reloads the network inventory from disk.

        Discards the in-memory inventory cache and re-reads YAML files.
        Use after editing the inventory files to pick up changes.

        Returns:
            A ToolEnvelope with empty results and request-level success (the
            operation has no per-host output; see envelope.py for the
            empty-envelope choice).
        """
        reset_nornir()
        return ToolEnvelope(
            operation="nornir_reload_inventory",
            request_id=self._request_id(ctx),
            results={},
        )

    def nornir_backup_config(
        self,
        name: str | list[str] | None = None,
        group: str | None = None,
        platform: str | None = None,
        ctx: Context | None = None,
    ) -> ToolEnvelope:
        """Captures and stores the running configuration for device(s).

        Reads each device's running config (NAPALM, falling back to the CLI
        for platforms without a NAPALM driver) and stores it as an immutable
        backup. These backups are the rollback substrate for
        nornir_apply_config, which requires a pre-change backup before any
        change (spec §8.2). One host's capture failure does not stop the
        others. An audit line is appended (hashes only — never config text).

        Omit all filters to target every device in the inventory.

        Args:
            name: Device name or list of names to back up.
            group: Group name to filter devices by.
            platform: Platform name to filter devices by.

        Returns:
            A ToolEnvelope with one HostOutcome per device. Successful
            outcomes carry ``data = {"backup_id", "path", "sha256", "size",
            "timestamp"}``.
        """
        operation = "nornir_backup_config"
        request_id = self._request_id(ctx)

        error, targets = self._select_targets(operation, request_id, name, group, platform)
        if error is not None:
            return error
        assert targets is not None

        store = get_backup_store()
        outcomes: dict[str, HostOutcome] = {}
        shas: dict[str, str] = {}
        for host in targets.inventory.hosts.values():
            hostname = host.name
            try:
                content = capture_running_config(hostname, str(host.platform), request_id)
                record = store.save(hostname, content, trigger="standalone")
            except McpError as exc:
                outcomes[hostname] = outcome_from_mcp_error(exc)
            except Exception as exc:  # noqa: BLE001 — per-host isolation
                outcomes[hostname] = outcome_from_mcp_error(
                    InternalError(str(exc), host=hostname, operation=operation)
                )
            else:
                shas[hostname] = record.sha256
                outcomes[hostname] = HostOutcome(
                    success=True,
                    data={
                        "backup_id": record.backup_id,
                        "path": record.path,
                        "sha256": record.sha256,
                        "size": record.size,
                        "timestamp": record.timestamp,
                    },
                )

        successes = sum(1 for o in outcomes.values() if o.success)
        if successes == len(outcomes):
            result = "success"
        elif successes == 0:
            result = "failed"
        else:
            result = "partial"
        get_audit_logger().record(
            operation,
            request_id,
            hosts=list(outcomes),
            result=result,
            details={"sha256": shas},
        )
        return ToolEnvelope(operation=operation, request_id=request_id, results=outcomes)

    def nornir_list_backups(self, host: str, ctx: Context | None = None) -> ToolEnvelope:
        """Lists the stored backups for one device, oldest first.

        These backups are the rollback substrate for nornir_apply_config's
        mandatory pre-change captures. Host names are validated against path
        traversal before any filesystem access.

        Args:
            host: Device name whose backups to list.

        Returns:
            A ToolEnvelope whose ``results["server"].data`` is the list of
            BackupRecord metadata (oldest first).
        """
        operation = "nornir_list_backups"
        request_id = self._request_id(ctx)

        try:
            records = get_backup_store().list(host)
        except ValidationError as exc:
            return self._validation_envelope(operation, request_id, exc.message)
        return ToolEnvelope(
            operation=operation,
            request_id=request_id,
            results={"server": HostOutcome(success=True, data=records)},
        )
