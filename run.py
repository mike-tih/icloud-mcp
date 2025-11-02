#!/usr/bin/env python3
"""
Entry point for iCloud MCP Server.

Usage:
    python run.py              # Run with stdio transport (local)
    python run.py --http       # Run with HTTP/SSE transport (server)
"""

import sys
import argparse


def main():
    parser = argparse.ArgumentParser(description="iCloud MCP Server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Run with HTTP/SSE transport instead of stdio"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for HTTP server (default: from env or 8000)"
    )

    args = parser.parse_args()

    # Import after parsing to allow --help without dependencies
    from src.icloud_mcp.server import run, run_http
    from src.icloud_mcp.config import config

    if args.http:
        if args.port:
            config.MCP_SERVER_PORT = args.port
        print(f"Starting iCloud MCP Server on HTTP port {config.MCP_SERVER_PORT}")
        run_http()
    else:
        print("Starting iCloud MCP Server with stdio transport", file=sys.stderr)
        run()


if __name__ == "__main__":
    main()
