# ABOUTME: Entry point for `python -m invoice_processing_mcp.server`.
# Runs the stdio MCP server defined in server.py.

import asyncio

from invoice_processing_mcp.server.server import main

if __name__ == "__main__":
    asyncio.run(main())
