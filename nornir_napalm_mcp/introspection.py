"""NAPALM getter introspection — discover supported getters per platform."""

from __future__ import annotations

import logging

import napalm

from nornir_napalm_mcp.models import GetterInfo
from nornir_napalm_mcp.runner import get_nornir

log = logging.getLogger("nornir-napalm-mcp")


def list_getters() -> list[GetterInfo]:
    """Lists available NAPALM getters for each platform in the inventory.

    Introspects the NAPALM driver for each unique platform to discover
    which getters are supported. No device connection is required.

    Returns:
        A list of GetterInfo objects, one per platform, each containing
        the platform name and a sorted list of available getter names.
    """
    nr = get_nornir()
    platforms = {str(host.platform) for host in nr.inventory.hosts.values()}

    results = []
    for platform in sorted(platforms):
        try:
            driver = napalm.get_network_driver(platform)
            getters = sorted(
                name.removeprefix("get_")
                for name in dir(driver)
                if name.startswith("get_") and callable(getattr(driver, name))
            )
        except Exception as e:
            log.warning("Could not introspect driver for platform '%s': %s", platform, e)
            getters = []
        results.append(GetterInfo(platform=platform, getters=getters))

    return results
