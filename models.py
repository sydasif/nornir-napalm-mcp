"""Pydantic data models for the Nornir-NAPALM MCP Server."""

from pydantic import BaseModel


class InventoryDevice(BaseModel):
    name: str
    hostname: str
    platform: str
    groups: list[str]


class GetterInfo(BaseModel):
    platform: str
    getters: list[str]
