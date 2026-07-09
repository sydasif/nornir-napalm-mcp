"""Pydantic data models for the Nornir-NAPALM MCP Server."""

from typing import Any

from pydantic import BaseModel


class InventoryDevice(BaseModel):
    name: str
    hostname: str
    platform: str
    groups: list[str]


class GetterInfo(BaseModel):
    platform: str
    getters: list[str]


class HostResult(BaseModel):
    """Per-host outcome of a Nornir task.

    Makes success/failure explicit in the return type rather than relying
    on callers to duck-type whether a given host's entry is real getter
    data or an error description.
    """

    ok: bool
    data: Any | None = None
    error: str | None = None
