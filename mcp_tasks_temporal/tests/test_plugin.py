# ABOUTME: Construction test for MCPTasksClientPlugin.
# Verifies it builds a named Temporal SimplePlugin and registers with a Worker via plugins=[...].

import asyncio

from mcp.client.stdio import StdioServerParameters
from temporalio.plugin import SimplePlugin
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.plugin import PLUGIN_NAME, MCPTasksClientPlugin

_PARAMS = StdioServerParameters(
    command="python", args=["-m", "invoice_processing_mcp.server"]
)


def test_plugin_constructs_as_named_simple_plugin():
    plugin = MCPTasksClientPlugin(_PARAMS)
    assert isinstance(plugin, SimplePlugin)
    assert plugin.name() == PLUGIN_NAME


def test_plugin_registers_workflow_and_activities_with_worker():
    # The plugin must inject TaskTrackerWorkflow + activities so a Worker needs no explicit lists.
    async def _build() -> None:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            worker = Worker(
                env.client,
                task_queue=models.TASK_QUEUE,
                plugins=[MCPTasksClientPlugin(_PARAMS)],
            )
            assert (
                worker is not None
            )  # construction succeeded with no explicit workflows/activities

    asyncio.run(_build())
