"""Nornir initialization for the MCP Server."""

import os
from functools import lru_cache
from pathlib import Path

from nornir import InitNornir
from nornir.core import Nornir


@lru_cache(maxsize=1)
def _get_nornir() -> Nornir:
    config_path = Path(os.environ.get("NORNIR_CONFIG", "config.yaml")).resolve()
    return InitNornir(config_file=str(config_path))


def reset_nornir() -> None:
    _get_nornir.cache_clear()
