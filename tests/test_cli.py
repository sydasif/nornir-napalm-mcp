"""Tests for main.py CLI entry points."""

from __future__ import annotations

import pytest


def test_main_help(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify main() accepts --help without crashing."""
    from nornir_napalm_mcp.main import main

    monkeypatch.setattr("sys.argv", ["nornir-napalm-mcp", "--help"])
    with pytest.raises(SystemExit, match="0"):
        main()


def test_main_stdio_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify main() with --transport stdio parses args correctly."""
    from nornir_napalm_mcp.main import main
    from nornir_napalm_mcp.server import mcp

    calls: list[tuple[str, dict[str, object]]] = []

    def mock_run(**kwargs: object) -> None:
        calls.append(("run", kwargs))

    monkeypatch.setattr("sys.argv", ["nornir-napalm-mcp", "--transport", "stdio"])
    monkeypatch.setattr(mcp, "run", mock_run)
    main()
    assert len(calls) == 1
    assert calls[0][0] == "run"
    assert calls[0][1]["transport"] == "stdio"


def test_main_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify main() with --transport http passes host and port."""
    from nornir_napalm_mcp.main import main
    from nornir_napalm_mcp.server import mcp

    calls: list[tuple[str, dict[str, object]]] = []

    def mock_run(**kwargs: object) -> None:
        calls.append(("run", kwargs))

    monkeypatch.setattr(
        "sys.argv",
        ["nornir-napalm-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "9000"],
    )
    monkeypatch.setattr(mcp, "run", mock_run)
    main()
    assert len(calls) == 1
    assert calls[0][1]["transport"] == "http"
    assert calls[0][1]["host"] == "0.0.0.0"
    assert calls[0][1]["port"] == 9000


def test_main_module_delegates_to_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify __main__.py calls main() at import time."""
    import importlib
    import sys

    called: list[bool] = []
    import nornir_napalm_mcp.main as main_module

    monkeypatch.setattr(main_module, "main", lambda: called.append(True))

    # Force reimport so __main__ executes its module-level main() call
    mod_name = "nornir_napalm_mcp.__main__"
    sys.modules.pop(mod_name, None)
    importlib.import_module(mod_name)

    assert called == [True]
