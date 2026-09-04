"""End-to-end tests through the real MCP protocol layer (step 19).

The in-memory fastmcp Client talks to the same server instance, so the
monkeypatched fakes stay active and every call crosses the full MCP
dispatch — argument validation, Context injection, and JSON
serialization included.

``call_tool(...).data`` is a fastmcp ``Root`` wrapper; ``dataclasses.asdict``
recursively converts it (and nested values) into plain dicts. The §21
``success`` property is NOT serialized over the wire (pydantic excludes
properties), so tests derive it: ``error is None and all host outcomes
succeeded``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from nornir_mcp import server
from nornir_mcp.core import audit, runner, storage
from tests.test_server import FROZEN_TOOL_NAMES


@pytest.fixture(autouse=True)
def _e2e_env(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Fresh fake Nornir, backup dir, and audit dir for every test."""
    request.getfixturevalue("fake_nornir")
    runner.reset_nornir()
    monkeypatch.setenv("NORNIR_MCP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("NORNIR_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    storage.reset_backup_store()
    audit.reset_audit_logger()
    yield
    runner.reset_nornir()
    storage.reset_backup_store()
    audit.reset_audit_logger()


async def _call(
    client: Client[Any], name: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Call a tool over the MCP protocol and return its envelope as plain dicts."""
    result = await client.call_tool(name, args or {})
    assert result.data is not None, f"tool '{name}' returned no structured data"
    return dataclasses.asdict(result.data)


def _derived_success(env: dict[str, Any]) -> bool:
    """The §21 iff invariant, computed over the wire payload."""
    return env.get("error") is None and all(
        outcome.get("success") for outcome in env.get("results", {}).values()
    )


@pytest.mark.anyio
async def test_e2e_tool_registry_has_exactly_twelve_nornir_tools(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """The wire registry exposes exactly the 12 frozen nornir_* tools."""
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
        names = sorted(tool.name for tool in tools)
        assert names == sorted(FROZEN_TOOL_NAMES)
        assert len(names) == 12
        assert all(name.startswith("nornir_") for name in names)


@pytest.mark.anyio
async def test_e2e_read_workflow(netmiko_fakes: list[dict[str, Any]]) -> None:
    """Inventory listing and a read-only command work over the protocol."""
    async with Client(server.mcp) as client:
        env = await _call(client, "nornir_list_inventory")
        assert env["operation"] == "nornir_list_inventory"
        devices = env["results"]["server"]["data"]
        assert len(devices) == 2

        env = await _call(client, "nornir_run_command", {"command": "show version"})
        assert _derived_success(env)
        spine = env["results"]["spine-01"]
        assert spine["success"] is True
        assert spine["data"]["command"] == "show version"
        assert "canned output" in spine["data"]["output"]
        assert spine["data"]["truncated"] is False


@pytest.mark.anyio
async def test_e2e_policy_blocks_write_via_read_tool(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """A DANGEROUS command is rejected per host and never reaches a device."""
    async with Client(server.mcp) as client:
        before = [c for c in netmiko_fakes if c["function"] == "netmiko_send_command"]
        env = await _call(client, "nornir_run_command", {"command": "reload"})
        assert not _derived_success(env)
        for host, outcome in env["results"].items():
            assert outcome["success"] is False
            assert outcome["error"]["type"] == "command_rejected"
        after = [c for c in netmiko_fakes if c["function"] == "netmiko_send_command"]
        assert after == before == []


@pytest.mark.anyio
async def test_e2e_dry_run_then_apply_full_lifecycle(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """Dry run plans without touching devices; real apply then saves."""
    config = ["interface Ethernet1", "description uplink"]
    async with Client(server.mcp) as client:
        env = await _call(client, "nornir_apply_config", {"config": config})
        assert _derived_success(env)
        for outcome in env["results"].values():
            assert outcome["data"]["dry_run_mode"] == "planned_commands_only"
        assert [c for c in netmiko_fakes if c["function"] == "netmiko_send_config"] == []

        env = await _call(client, "nornir_apply_config", {"config": config, "dry_run": False})
        assert _derived_success(env)
        for outcome in env["results"].values():
            assert outcome["data"]["applied"] == config
        assert len([c for c in netmiko_fakes if c["function"] == "netmiko_send_config"]) == 2

        env = await _call(client, "nornir_save_config")
        assert _derived_success(env)
        assert len([c for c in netmiko_fakes if c["function"] == "netmiko_save_config"]) == 2


@pytest.mark.anyio
async def test_e2e_backup_and_audit_artifacts_exist_with_correct_permissions(
    netmiko_fakes: list[dict[str, Any]], tmp_path: Path
) -> None:
    """Apply leaves immutable 0600 backups with sidecars and an audited change."""
    async with Client(server.mcp) as client:
        env = await _call(
            client,
            "nornir_apply_config",
            {"config": ["interface Ethernet1"], "dry_run": False},
        )
        change_id = env["results"]["spine-01"]["data"]["change_id"]
        assert change_id.startswith("chg-")

        backups = tmp_path / "backups"
        for host in ("spine-01", "leaf-01"):
            cfg = list((backups / host).glob("*.cfg"))
            meta = list((backups / host).glob("*.meta.json"))
            assert len(cfg) == 1, f"{host} missing .cfg backup"
            assert len(meta) == 1, f"{host} missing .meta.json sidecar"
            if os.name == "posix":
                assert stat.S_IMODE(cfg[0].stat().st_mode) == 0o600
                assert stat.S_IMODE(meta[0].stat().st_mode) == 0o600

        audit_lines = (tmp_path / "audit" / "audit.jsonl").read_text().splitlines()
        assert any(json.loads(line)["change_id"] == change_id for line in audit_lines)


@pytest.mark.anyio
async def test_e2e_no_secrets_in_responses(netmiko_fakes: list[dict[str, Any]]) -> None:
    """Sanitized responses never expose the fake config's secret (§22)."""
    async with Client(server.mcp) as client:
        env = await _call(client, "nornir_get_config")
        payload = json.dumps(env)
        assert "enable secret" not in payload
        assert "$1$e2e$f00" not in payload
        # Genuine test: the fake really carries the secret — an explicit
        # unsanitized request exposes it, proving sanitize is what strips it.
        env_raw = await _call(client, "nornir_get_config", {"sanitized": False})
        assert "$1$e2e$f00" in json.dumps(env_raw)


@pytest.mark.anyio
async def test_e2e_envelope_invariant_across_workflow(
    netmiko_fakes: list[dict[str, Any]],
) -> None:
    """Every envelope obeys §21: success iff no request error and all hosts ok."""
    scenarios: list[tuple[str, dict[str, Any], bool]] = [
        ("nornir_list_inventory", {}, True),
        ("nornir_run_command", {"command": "show version"}, True),
        ("nornir_run_command", {"command": "reload"}, False),
        ("nornir_apply_config", {"config": ["interface Ethernet1"]}, True),
        (
            "nornir_apply_config",
            {"config": ["interface Ethernet1"], "dry_run": False},
            True,
        ),
        ("nornir_save_config", {}, True),
        ("nornir_get_config", {}, True),
        ("nornir_list_backups", {"host": "spine-01"}, True),
    ]
    async with Client(server.mcp) as client:
        for name, args, expected in scenarios:
            env = await _call(client, name, args)
            derived = _derived_success(env)
            assert derived == expected, (
                f"'{name}' violated the §21 invariant: expected success={expected}, "
                f"got error={env.get('error')}, results={env.get('results')}"
            )
