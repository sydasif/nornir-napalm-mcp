"""NAPALM getter introspection — discover supported getters per platform."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

import napalm
from napalm.base import NetworkDriver
from nornir_mcp.core.runner import get_nornir


class GetterInfo(BaseModel):
    """The NAPALM getters available for a given platform.

    ``error`` is set (with ``getters`` empty) when the platform's driver
    could not be introspected.
    """

    model_config = ConfigDict(frozen=True)

    platform: str
    getters: list[str]
    error: str | None = None


def list_getters() -> list[GetterInfo]:
    """Lists available NAPALM getters for each platform in the inventory.

    Introspects each platform's NAPALM driver class and reports only
    getters the driver actually overrides. Base-class stubs that merely
    raise ``NotImplementedError`` are excluded so unsupported getters are
    never advertised. Caveat: a handful of base-level generic getters
    implemented directly on ``NetworkDriver`` may be excluded as a side
    effect — under-reporting is the safe failure mode for a discovery tool.

    Hosts with ``platform=None`` are skipped entirely. Platforms whose
    driver cannot be introspected surface the failure in the returned
    :class:`GetterInfo.error` field instead of an empty list plus a log
    line.

    Returns:
        A list of GetterInfo objects, one per unique platform, each
        containing the platform name, a sorted list of overridden getter
        names (unprefixed), and an optional error description.
    """
    nr = get_nornir()
    platforms = {
        str(host.platform) for host in nr.inventory.hosts.values() if host.platform is not None
    }

    results: list[GetterInfo] = []
    for platform in sorted(platforms):
        error: str | None = None
        try:
            driver_cls = napalm.get_network_driver(platform)
            getters = sorted(
                name.removeprefix("get_")
                for name in dir(driver_cls)
                if name.startswith("get_")
                and callable(getattr(driver_cls, name))
                and getattr(driver_cls, name) is not getattr(NetworkDriver, name, None)
            )
        except Exception as exc:
            getters = []
            error = str(exc)
        results.append(GetterInfo(platform=platform, getters=getters, error=error))

    return results
