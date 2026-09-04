"""Nornir-NAPALM FastMCP Server — CLI entry point."""

from __future__ import annotations

import argparse
import sys

from nornir_napalm_mcp.server import mcp


def main(argv: list[str] | None = None) -> None:
    """Run the MCP server with the specified transport.

    Args:
        argv: Optional command-line arguments. If None, sys.argv is used.
    """
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="nornir-napalm-mcp",
        description="Query network devices via Nornir + NAPALM over MCP.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport protocol (default: stdio).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (HTTP transport).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (HTTP transport).")
    args = parser.parse_args(argv)

    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
