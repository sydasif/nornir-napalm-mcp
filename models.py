"""Pydantic data models for the Nornir-NAPALM MCP Server."""

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


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

    @field_validator("hostname", "vendor", "model", "os_version", "serial_number", mode="before")
    @classmethod
    def coerce_to_str(cls, v: Any) -> str | None:
        """Coerce non-string NAPALM data to str."""
        if v is None:
            return None
        return str(v)


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


class PingStats(BaseModel):
    """Statistics from a single ping attempt."""

    packets_sent: int
    packets_received: int
    rtt_min: float | None = None
    rtt_max: float | None = None
    rtt_avg: float | None = None
    packet_loss: float | None = None

    @model_validator(mode="after")
    def compute_loss(self) -> "PingStats":
        """Calculate packet loss percentage if not directly provided."""
        if self.packet_loss is None and self.packets_sent > 0:
            self.packet_loss = max(
                0.0,
                round(((self.packets_sent - self.packets_received) / self.packets_sent) * 100, 2),
            )
        return self


class PingResult(BaseModel):
    """Result of a ping operation from a network device."""

    destination: str
    success: bool
    stats: PingStats | None = None
    error: str | None = None

    @classmethod
    def from_napalm(cls, destination: str, data: dict[str, Any]) -> "PingResult":
        """Factory method to create a PingResult from raw NAPALM data."""
        # Check error first: any response with an error key is a failure,
        # even if a success key is also present (C14).
        if "error" in data:
            return cls(
                destination=destination,
                success=False,
                error=data.get("error", "Unknown ping failure"),
            )

        # Validate success payload is a dict before indexing (C1).
        success_data = data.get("success")
        if not isinstance(success_data, dict):
            return cls(
                destination=destination,
                success=False,
                error=f"Unexpected ping response: {type(success_data).__name__}",
            )

        return cls(
            destination=destination,
            success=True,
            stats=PingStats(
                packets_sent=success_data.get("packets_sent", 0),
                packets_received=success_data.get("packets_received", 0),
                packet_loss=success_data.get("packet_loss"),
                rtt_min=success_data.get("rtt_min"),
                rtt_max=success_data.get("rtt_max"),
                rtt_avg=success_data.get("rtt_avg"),
            ),
        )
