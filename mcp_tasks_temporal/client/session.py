# ABOUTME: Owns the v2 mcp.client stdio session and issues tasks-extension requests with the capability _meta.
# The session is established once for the worker's lifetime (via the Temporal plugin's run_context).

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from mcp_tasks_temporal._sdk_compat import install_tasks_result_passthrough
from mcp_tasks_temporal.wire import client_capability_meta


@asynccontextmanager
async def connect_tasks_session(
    server_params: StdioServerParameters,
) -> AsyncIterator[ClientSession]:
    """Launch the MCP server over stdio, initialize, and yield a ready ClientSession.

    Installs the client-side compat shim so a bare CreateTaskResult passes inbound validation.
    """
    install_tasks_result_passthrough()
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def task_request(
    session: ClientSession, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Issue a tasks-extension request, adding the per-request capability `_meta`.

    Uses the session dispatcher directly: the tasks/* methods are unknown to the SDK's
    typed request/result models, and the dispatcher is the raw JSON-RPC seam.
    """
    payload = dict(params)
    payload["_meta"] = client_capability_meta()
    return await session._dispatcher.send_raw_request(method, payload, {})
