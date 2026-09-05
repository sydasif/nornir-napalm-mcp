"""Nornir-NAPALM FastMCP Server — composition root.

Tool definitions live in ``tools/{base,napalm,netmiko}/tool.py``; this file
instantiates them and registers the resulting twelve bound methods on the
MCP server. The CLI entry point is ``nornir_mcp.cli.main:main``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from nornir_mcp.tools.base.tool import NornirBase
from nornir_mcp.tools.napalm.tool import NapalmTool
from nornir_mcp.tools.netmiko.tool import NetmikoTool

mcp = FastMCP(
    name="Nornir-NAPALM Server",
    instructions="Query network devices via NAPALM. Call nornir_list_inventory first.",
)

_nornir_base = NornirBase()
_napalm_tools = NapalmTool()
_netmiko_tools = NetmikoTool()

_TOOLS: tuple[Any, ...] = (
    # base — server tools
    _nornir_base.nornir_list_inventory,
    _nornir_base.nornir_reload_inventory,
    _nornir_base.nornir_backup_config,
    _nornir_base.nornir_list_backups,
    # napalm — read/getter tools
    _napalm_tools.nornir_get_facts,
    _napalm_tools.nornir_run_getter,
    _napalm_tools.nornir_get_config,
    _napalm_tools.nornir_list_getters,
    # netmiko — CLI/read and write tools
    _netmiko_tools.nornir_run_command,
    _netmiko_tools.nornir_run_commands,
    _netmiko_tools.nornir_apply_config,
    _netmiko_tools.nornir_save_config,
)

for _tool in _TOOLS:  # bound methods: `self` is bound,
    mcp.tool(name=_tool.__name__)(_tool)  # so the schema has no self param
