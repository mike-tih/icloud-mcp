#!/usr/bin/env python3
"""
Entry point for iCloud MCP Server.

Usage:
    python run.py              # Run with stdio transport (local)
    python run.py --http       # Run with HTTP/SSE transport (server)
"""

import sys
import argparse
import os


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
    try:
        # Try installed package first (for Docker/production)
        from icloud_mcp.server import mcp
        from icloud_mcp.config import config
    except ImportError:
        # Fall back to development mode
        from src.icloud_mcp.server import mcp
        from src.icloud_mcp.config import config

    if args.http:
        port = args.port or int(os.environ.get("PORT", config.MCP_SERVER_PORT))
        print(f"Starting iCloud MCP Server on HTTP port {port}")
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        print("Starting iCloud MCP Server with stdio transport", file=sys.stderr)
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
