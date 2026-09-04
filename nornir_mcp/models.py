"""Pydantic data models for the Nornir-NAPALM MCP Server."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class InventoryDevice(BaseModel):
    """A network device in the Nornir inventory."""

    model_config = ConfigDict(frozen=True)

    name: str
    hostname: str
    platform: str
    groups: list[str]


class GetterInfo(BaseModel):
    """The NAPALM getters available for a given platform.

    ``error`` is set (with ``getters`` empty) when the platform's driver
    could not be introspected.
    """

    model_config = ConfigDict(frozen=True)

    platform: str
    getters: list[str]
    error: str | None = None
