# ABOUTME: MCPTasksClientPlugin packages the durable client (workflow + wire activities + MCP session)
# as a Temporal Plugin, so a consumer adopts the whole tasks client with one plugins=[...] entry.

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.client.stdio import StdioServerParameters
from temporalio.plugin import SimplePlugin

from mcp_tasks_temporal.client.activities import MCPActivities
from mcp_tasks_temporal.client.session import connect_tasks_session
from mcp_tasks_temporal.client.workflows import TaskTrackerWorkflow

PLUGIN_NAME = "io.temporal.mcp-tasks-client"


def MCPTasksClientPlugin(
    server_params: StdioServerParameters, *, name: str = PLUGIN_NAME
) -> SimplePlugin:
    """A Temporal Plugin bundling the durable MCP-tasks client.

    Registers TaskTrackerWorkflow and the wire activities, and — via run_context — opens the MCP
    stdio session for the worker's lifetime and binds it to the activities. Usage:

        Worker(client, task_queue=models.TASK_QUEUE, plugins=[MCPTasksClientPlugin(server_params)])
    """
    activities = MCPActivities()

    @asynccontextmanager
    async def run_context() -> AsyncIterator[None]:
        async with connect_tasks_session(server_params) as session:
            activities.bind(session)
            try:
                yield
            finally:
                activities.bind(None)

    return SimplePlugin(
        name,
        workflows=[TaskTrackerWorkflow],
        activities=activities.activity_callables(),
        run_context=run_context,
    )
