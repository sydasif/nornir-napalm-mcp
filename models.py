"""Pydantic data models for the Nornir-NAPALM MCP Server."""

from typing import Any

from pydantic import BaseModel, Field


class InventoryDevice(BaseModel):
    """Structured representation of a network device in the inventory."""

    name: str
    hostname: str
    platform: str
    groups: list[str]


class NetworkFacts(BaseModel):
    """System facts for a network device."""

    hostname: str | None = None
    vendor: str | None = None
    model: str | None = None
    os_version: str | None = None
    serial_number: str | None = None
    additional_facts: dict[str, Any] = Field(default_factory=dict)


class NetworkInterfaces(BaseModel):
    """Interface and IP address data for a network device."""

    interfaces: dict[str, Any] = Field(default_factory=dict)
    interfaces_ip: dict[str, Any] = Field(default_factory=dict)


class DeviceConfig(BaseModel):
    """Running and/or startup configuration for a network device."""

    running: str | None = None
    startup: str | None = None


class ReloadSummary(BaseModel):
    """Summary of inventory reload changes."""

    previous_hosts: list[str]
    current_hosts: list[str]
    added: list[str]
    removed: list[str]
    total: int


class GetterInfo(BaseModel):
    """Available NAPALM getters for a given platform."""

    platform: str
    getters: list[str]
