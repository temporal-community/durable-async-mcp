# ABOUTME: Allows running the MCP client as a module via `python -m mcp_client`.
# Delegates to main.main().

import asyncio
import sys

from async_mcp.mcp_client.main import main

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nGoodbye!")
    sys.exit(0)
