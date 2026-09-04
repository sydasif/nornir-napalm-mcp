"""Nornir initialization for the MCP Server."""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from nornir import InitNornir
from nornir.core import Nornir

# Keys whose string values are file paths that should be resolved relative
# to the config file directory.
_PATH_KEYS: frozenset[str] = frozenset(
    {
        "host_file",
        "group_file",
        "defaults_file",
        "config_file",
        "log_file",
    }
)


@runtime_checkable
class NornirLike(Protocol):
    """Structural interface the task layer relies on.

    Declares the minimal Nornir surface (filtered inventory access,
    device filtering, task execution) so the task helpers accept the real
    Nornir instance or a fake without importing the concrete class.
    """

    @property
    def inventory(self) -> Any:
        """The inventory containing all known hosts."""
        ...

    data: Any

    def filter(  # noqa: A003 - matches Nornir's API name
        self,
        *,
        filter_func: Any = None,  # noqa: ARG002
        platform: str | None = None,
    ) -> NornirLike:
        """Return a new instance containing only matching devices.

        Args:
            filter_func: Callable that receives a host and returns whether it
                matches.
            platform: Only keep hosts with this platform.

        Returns:
            A new instance with only the matching devices.
        """
        ...

    def run(self, task: Any, **task_kwargs: Any) -> Any:
        """Execute a task against the filtered inventory and return its results.

        Args:
            task: The task function to execute.

        Other Parameters:
            **task_kwargs: Additional keyword arguments passed to the task.

        Returns:
            The aggregated result of running the task.
        """
        ...


@cache
def get_nornir() -> Nornir:
    """Return the process-wide Nornir singleton, initializing it if needed.

    The configuration file path is read from the ``NORNIR_CONFIG`` environment
    variable. ``~`` and ``$VAR`` are expanded in the config path itself, and
    inside ``config.yaml`` all string values have ``~``, ``$VAR``, and
    environment-variable references expanded. Known path keys are resolved
    against the *config.yaml* directory rather than the process working
    directory.

    Uses ``functools.cache`` to ensure single initialization so that concurrent
    first callers don't race to build separate Nornir instances.

    Returns:
        The process-wide Nornir singleton instance.
    """
    config_path = _resolve_config_path()
    expanded = _load_config(config_path)
    return InitNornir(**expanded)


def reset_nornir() -> None:
    """Clear the cached Nornir instance so the next call reloads from disk."""
    get_nornir.cache_clear()


def _expand_str(value: str) -> str:
    """Expand ``~`` and ``$VAR``/``${VAR}`` references in a string.

    ``~`` is expanded first (``expanduser``), then environment variables
    (``expandvars``), mirroring shell-style expansion.

    Args:
        value: The string to expand.

    Returns:
        The string with ``~`` and environment variables expanded.
    """
    return os.path.expandvars(os.path.expanduser(value))


def _expand_config(value: object, config_dir: Path) -> object:
    """Recursively expand ``~`` and ``$VAR`` in configuration strings.

    ``~`` and environment variables (``$HOME``, ``${VAR}``) are expanded in
    all string values. Only values belonging to known path keys (e.g.
    ``host_file``) are additionally resolved against *config_dir* so that
    relative inventory paths work regardless of the server's working directory.

    Args:
        value: A configuration value (str, dict, list, or scalar).
        config_dir: The directory containing ``config.yaml`` (the anchor for
            relative paths).

    Returns:
        The expanded value with the same type as *value*.
    """
    match value:
        case str():
            return _expand_str(value)
        case dict():
            return {k: _expand_config_key(k, v, config_dir) for k, v in value.items()}
        case list():
            return [_expand_config(v, config_dir) for v in value]
        case _:
            return value


def _expand_config_key(key: str, value: object, config_dir: Path) -> object:
    """Expand one config value, resolving relative paths for known path keys.

    Args:
        key: The configuration key (used to detect known path keys).
        value: The configuration value to expand.
        config_dir: The directory containing ``config.yaml`` (the anchor for
            relative paths).

    Returns:
        The expanded value; relative paths for known path keys are resolved
        against *config_dir*.
    """
    if isinstance(value, str) and key in _PATH_KEYS:
        expanded = _expand_str(value)
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

    Returns:
        The absolute path to the Nornir configuration file.

    Raises:
        FileNotFoundError: If ``NORNIR_CONFIG`` is unset or points to a
            missing file.
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
            "Nornir config file not found. Verify NORNIR_CONFIG points to a valid config.yaml."
        )
    return config_path


def _load_config(config_path: Path) -> dict[str, Any]:
    """Load and expand a Nornir configuration file.

    All string values in the YAML file have ``~`` and ``$VAR`` expanded.
    Known path keys (host_file, group_file, etc.) are resolved relative to
    *config_path*'s parent directory.

    Args:
        config_path: Absolute path to the Nornir configuration file.

    Returns:
        A dictionary suitable for passing as ``**kwargs`` to ``InitNornir``.

    Raises:
        TypeError: If the expanded configuration is not a mapping.
    """
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text()) or {}
    result = _expand_config(raw, config_path.parent)
    if not isinstance(result, dict):
        raise TypeError(f"Expected dict from config expansion, got {type(result).__name__}")
    return result
