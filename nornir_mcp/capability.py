"""Platform capability gate for CLI tools.

Maps NAPALM-style platform names to netmiko ``device_type`` values so
tools that drive device CLIs can open sessions — and, crucially, fail
with a clear capability error *before* any device I/O when the target
platform is unsupported.
"""

from __future__ import annotations

from nornir_mcp.errors import UnsupportedOperationError

# netmiko device_type per supported platform (CLI tooling only supports
# these; anything else is an explicit capability error, never a guess).
NETMIKO_DEVICE_TYPES: dict[str, str] = {
    "ios": "cisco_ios",
    "eos": "arista_eos",
}


def netmiko_device_type(platform: str) -> str:
    """Return the netmiko device_type for *platform*.

    Args:
        platform: NAPALM-style platform name.

    Returns:
        The netmiko device_type (e.g. ``"cisco_ios"`` for ``"ios"``).

    Raises:
        UnsupportedOperationError: If *platform* has no netmiko mapping.
            The message lists the supported platforms.
    """
    device_type = NETMIKO_DEVICE_TYPES.get(platform)
    if device_type is None:
        supported = ", ".join(sorted(NETMIKO_DEVICE_TYPES))
        raise UnsupportedOperationError(
            f"CLI access is unsupported for platform '{platform}'. "
            f"Supported platforms: {supported}."
        )
    return device_type


def supports_cli(platform: str) -> bool:
    """True if *platform* has a netmiko device_type mapping."""
    return platform in NETMIKO_DEVICE_TYPES
