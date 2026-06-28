# ABOUTME: MCPTasksClientPlugin packages the durable client (workflow + wire activities + FastMCP session)
# as a Temporal Plugin, so a consumer adopts the whole tasks client with one plugins=[...] entry.

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from temporalio.client import Client as TemporalClient
from temporalio.plugin import SimplePlugin

from mcp_tasks_temporal.client.activities import MCPActivities
from mcp_tasks_temporal.client.session import connect_tasks_session
from mcp_tasks_temporal.client.workflows import TaskTrackerWorkflow

PLUGIN_NAME = "io.temporal.mcp-tasks-client"


def MCPTasksClientPlugin(
    config: dict[str, Any],
    temporal_client: TemporalClient,
    *,
    name: str = PLUGIN_NAME,
) -> SimplePlugin:
    """A Temporal Plugin bundling the durable MCP-tasks client.

    Registers TaskTrackerWorkflow and the wire activities, and — via run_context — opens the
    FastMCP client (wired to the activities' elicitation handler) for the worker's lifetime.
    `temporal_client` lets the elicitation handler signal/query the tracker workflows from
    FastMCP's receive loop (which has no Temporal activity context). Usage:

        Worker(client, task_queue=models.TASK_QUEUE, plugins=[MCPTasksClientPlugin(config, client)])
    """
    activities = MCPActivities(temporal_client=temporal_client)

    @asynccontextmanager
    async def run_context() -> AsyncIterator[None]:
        async with connect_tasks_session(
            config, activities._elicitation_handler
        ) as mcp:
            activities.bind_mcp(mcp)
            try:
                yield
            finally:
                activities.bind_mcp(None)

    return SimplePlugin(
        name,
        workflows=[TaskTrackerWorkflow],
        activities=activities.activity_callables(),
        run_context=run_context,
    )
