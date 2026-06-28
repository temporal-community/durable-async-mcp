# ABOUTME: Owns the FastMCP client session used by the durable client's wire activities.
# Constructed once for the worker's lifetime (via the Temporal plugin's run_context).

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import Client as FastMCPClient


@asynccontextmanager
async def connect_tasks_session(
    config: dict[str, Any],
    elicitation_handler: Callable[..., Awaitable[Any]],
) -> AsyncIterator[Any]:
    """Open a FastMCP client against a Claude Desktop-format config and yield it.

    The elicitation_handler receives the server's `elicitation/create` pushes (during tasks/result)
    and routes each to the right TaskTrackerWorkflow.
    """
    async with FastMCPClient(config, elicitation_handler=elicitation_handler) as mcp:
        yield mcp
