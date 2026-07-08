"""Nornir initialization for the MCP Server."""

import os
from functools import lru_cache
from pathlib import Path

import yaml
from nornir import InitNornir
from nornir.core import Nornir

# Keys whose string values are file paths that should be resolved relative
# to the config file directory.
_PATH_KEYS = frozenset(
    {
        "host_file",
        "group_file",
        "defaults_file",
        "config_file",
        "log_file",
    }
)


def _expand_config(value: object, config_dir: Path) -> object:
    """Recursively expand ``~`` and ``$VAR`` in configuration strings.

    ``~`` and environment variables (``$HOME``, ``${VAR}``) are expanded in
    all string values.  Only values belonging to known path keys (e.g.
    ``host_file``) are additionally resolved against *config_dir* so that
    relative inventory paths work regardless of the server's working directory.

    Args:
        value: A configuration value (str, dict, list, or scalar).
        config_dir: The directory containing ``config.yaml`` (the anchor for
            relative paths).

    Returns:
        The expanded value with the same type as *value*.
    """
    if isinstance(value, str):
        expanded = os.path.expandvars(os.path.expanduser(value))
        return expanded
    if isinstance(value, dict):
        return {k: _expand_config_key(k, v, config_dir) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_config(v, config_dir) for v in value]
    return value


def _expand_config_key(key: str, value: object, config_dir: Path) -> object:
    """Like ``_expand_config`` but resolves relative paths for known path keys."""
    if isinstance(value, str) and key in _PATH_KEYS:
        expanded = os.path.expandvars(os.path.expanduser(value))
        if not os.path.isabs(expanded):
            expanded = str((config_dir / expanded).resolve())
        return expanded
    return _expand_config(value, config_dir)


def _resolve_config_path() -> Path:
    """Resolve the Nornir configuration file path.

    The server **requires** the ``NORNIR_CONFIG`` environment variable to be set.
    No automatic fallback to a ``config.yaml`` file in the cwd is performed.
    If the variable is missing or the referenced file does not exist, a clear
    ``FileNotFoundError`` is raised to guide the user.
    """
    config_env = os.environ.get("NORNIR_CONFIG")
    if not config_env:
        raise FileNotFoundError(
            "NORNIR_CONFIG environment variable is required to locate the Nornir "
            "configuration file. Set NORNIR_CONFIG to the absolute path of a "
            "valid config.yaml (e.g., export NORNIR_CONFIG=/path/to/config.yaml)."
        )
    # Expand user and env vars then resolve to absolute path
    config_path = Path(os.path.expandvars(config_env)).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Nornir config file not found at path specified by NORNIR_CONFIG: {config_path}"
        )
    return config_path


def _load_config(config_path: Path) -> dict:
    """Load and expand a Nornir configuration file.

    All string values in the YAML file have ``~`` and ``$VAR`` expanded.
    Known path keys (host_file, group_file, etc.) are resolved relative to
    *config_path*'s parent directory.

    Args:
        config_path: Absolute path to the Nornir configuration file.

    Returns:
        A dictionary suitable for passing as ``**kwargs`` to ``InitNornir``.
    """
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    return _expand_config(config, config_path.parent)


@lru_cache(maxsize=1)
def _get_nornir() -> Nornir:
    """Initialize and return a Nornir instance.

    The configuration file path is read from the ``NORNIR_CONFIG`` environment
    variable (default: ``config.yaml``).  ``~`` and ``$VAR`` are expanded in
    the config path itself.

    Inside ``config.yaml`` all string values have ``~``, ``$VAR``, and
    environment-variable references expanded.  Known path keys are resolved
    against the *config.yaml* directory rather than the process working
    directory.
    """
    config_path = _resolve_config_path()

    if not config_path.exists():
        raise ValueError(
            f"No Nornir config found at {config_path}. "
            "Set NORNIR_CONFIG or create config.yaml in the working directory."
        )

    expanded = _load_config(config_path)
    return InitNornir(**expanded)


def reset_nornir() -> None:
    """Clear the cached Nornir instance so the next call reloads from disk."""
    _get_nornir.cache_clear()
