"""Nornir-NAPALM FastMCP Server — CLI entry point."""

from __future__ import annotations

import argparse
import sys

from nornir_napalm_mcp.server import mcp


def main(
    *,
    transport: str | None = None,
    host: str | None = None,
    port: int | None = None,
    argv: list[str] | None = None,
) -> None:
    """Run the MCP server with the specified transport.

    Args:
        transport: Transport protocol ("stdio" or "http").
        host: Host to bind to (for HTTP transport).
        port: Port to bind to (for HTTP transport).
        argv: Optional command-line arguments. If None, sys.argv is used.
    """
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    # Use explicit args if provided, otherwise fall back to parsed args
    transport = transport or args.transport
    host = host or args.host
    port = port or args.port

    if transport == "http":
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
