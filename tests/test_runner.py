"""Tests for runner.py — config loading, path expansion, and singleton init."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nornir_napalm_mcp import runner
from tests.conftest import FakeHost


class TestExpandConfig:
    """Tests for _expand_config and _expand_config_key."""

    def test_str_expansion(self, tmp_path: Path) -> None:
        """String values have ~ and $VAR expanded."""
        os.environ["TEST_RUNNER_VAR"] = "expanded_val"
        result = runner._expand_config("$TEST_RUNNER_VAR", tmp_path)
        assert result == "expanded_val"
        del os.environ["TEST_RUNNER_VAR"]

    def test_list_expansion(self, tmp_path: Path) -> None:
        """Lists recurse into _expand_config for each element."""
        os.environ["TEST_RUNNER_VAR"] = "val"
        result = runner._expand_config(["$TEST_RUNNER_VAR", "literal"], tmp_path)
        assert result == ["val", "literal"]
        del os.environ["TEST_RUNNER_VAR"]

    def test_dict_expansion(self, tmp_path: Path) -> None:
        """Dicts recurse via _expand_config_key."""
        result = runner._expand_config({"key": "value"}, tmp_path)
        assert result == {"key": "value"}

    def test_non_string_passthrough(self, tmp_path: Path) -> None:
        """Non-string, non-collection values pass through unchanged."""
        assert runner._expand_config(42, tmp_path) == 42
        assert runner._expand_config(None, tmp_path) is None
        assert runner._expand_config(True, tmp_path) is True


class TestExpandConfigKey:
    """Tests for _expand_config_key path resolution."""

    def test_known_path_key_resolves_relative(self, tmp_path: Path) -> None:
        """Known path keys (host_file etc.) resolve relative to config dir."""
        result = runner._expand_config_key("host_file", "inventory/hosts.yaml", tmp_path)
        assert result == str((tmp_path / "inventory" / "hosts.yaml").resolve())

    def test_known_path_key_absolutepassthrough(self, tmp_path: Path) -> None:
        """Absolute paths in known path keys are not modified."""
        result = runner._expand_config_key("host_file", "/etc/hosts.yaml", tmp_path)
        assert result == "/etc/hosts.yaml"

    def test_known_path_key_expands_env(self, tmp_path: Path) -> None:
        """Known path keys expand $VAR references."""
        os.environ["TEST_HOST_DIR"] = "my_hosts"
        result = runner._expand_config_key("host_file", "$TEST_HOST_DIR/hosts.yaml", tmp_path)
        assert result == str((tmp_path / "my_hosts" / "hosts.yaml").resolve())
        del os.environ["TEST_HOST_DIR"]

    def test_unknown_key_no_resolve(self, tmp_path: Path) -> None:
        """Unknown keys don't resolve relative to config dir."""
        result = runner._expand_config_key("plugin", "SimpleInventory", tmp_path)
        assert result == "SimpleInventory"

    def test_nested_dict_in_config(self, tmp_path: Path) -> None:
        """Full config structure with nested dicts and known path keys."""
        config = {
            "inventory": {
                "plugin": "SimpleInventory",
                "options": {
                    "host_file": "inventory/hosts.yaml",
                    "group_file": "inventory/groups.yaml",
                },
            },
            "runner": {"plugin": "threaded"},
        }
        result = runner._expand_config(config, tmp_path)
        inv = result["inventory"]["options"]
        assert inv["host_file"] == str((tmp_path / "inventory" / "hosts.yaml").resolve())
        assert inv["group_file"] == str((tmp_path / "inventory" / "groups.yaml").resolve())
        assert result["inventory"]["plugin"] == "SimpleInventory"
        assert result["runner"]["plugin"] == "threaded"


class TestResolveConfigPath:
    """Tests for _resolve_config_path."""

    def test_missing_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing NORNIR_CONFIG raises FileNotFoundError."""
        monkeypatch.delenv("NORNIR_CONFIG", raising=False)
        with pytest.raises(FileNotFoundError, match="NORNIR_CONFIG"):
            runner._resolve_config_path()

    def test_nonexistent_file_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NORNIR_CONFIG pointing to missing file raises FileNotFoundError."""
        monkeypatch.setenv("NORNIR_CONFIG", "/nonexistent/path.yaml")
        with pytest.raises(FileNotFoundError, match="Nornir config file not found"):
            runner._resolve_config_path()


class TestLoadConfig:
    """Tests for _load_config."""

    def test_empty_config(self, tmp_path: Path) -> None:
        """Empty YAML file returns empty dict."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("---\n")
        result = runner._load_config(config_file)
        assert result == {}

    def test_env_var_in_config(self, tmp_path: Path) -> None:
        """Environment variables in config values are expanded."""
        os.environ["TEST_INVENTORY_DIR"] = "my_inventory"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "inventory:\n"
            "  plugin: SimpleInventory\n"
            "  options:\n"
            "    host_file: $TEST_INVENTORY_DIR/hosts.yaml\n"
        )
        result = runner._load_config(config_file)
        host_file = result["inventory"]["options"]["host_file"]
        assert host_file == str((tmp_path / "my_inventory" / "hosts.yaml").resolve())
        del os.environ["TEST_INVENTORY_DIR"]


class TestGetNornir:
    """Tests for the singleton _get_nornir pattern."""

    def test_singleton_same_instance(
        self, fake_nornir: dict[str, FakeHost], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple _get_nornir calls return the same object."""
        runner.reset_nornir()
        nr1 = runner._get_nornir()
        nr2 = runner._get_nornir()
        assert nr1 is nr2

    def test_reset_creates_new_instance(
        self, fake_nornir: dict[str, FakeHost], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """reset_nornir() causes next _get_nornir() to create a fresh instance."""
        runner.reset_nornir()
        nr1 = runner._get_nornir()
        runner.reset_nornir()
        nr2 = runner._get_nornir()
        assert nr1 is not nr2
