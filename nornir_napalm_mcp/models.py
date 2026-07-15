"""Pydantic data models for the Nornir-NAPALM MCP Server."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class InventoryDevice(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    hostname: str
    platform: str
    groups: list[str]


class GetterInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: str
    getters: list[str]


class HostResult(BaseModel):
    """Per-host outcome of a Nornir task.

    Makes success/failure explicit in the return type rather than relying
    on callers to duck-type whether a given host's entry is real getter
    data or an error description.
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    data: Any | None = None
    error: str | None = None
