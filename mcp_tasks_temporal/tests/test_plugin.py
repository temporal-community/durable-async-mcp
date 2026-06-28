# ABOUTME: Construction test for MCPTasksClientPlugin.
# Verifies it builds a named Temporal SimplePlugin and registers with a Worker via plugins=[...].

import asyncio
from unittest.mock import MagicMock

from temporalio.plugin import SimplePlugin
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.plugin import PLUGIN_NAME, MCPTasksClientPlugin

_RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
)

_CONFIG = {
    "mcpServers": {
        "invoice_processor": {
            "command": "python",
            "args": ["-m", "invoice_processing_mcp.server"],
        }
    }
}


def test_plugin_constructs_as_named_simple_plugin():
    plugin = MCPTasksClientPlugin(_CONFIG, MagicMock())
    assert isinstance(plugin, SimplePlugin)
    assert plugin.name() == PLUGIN_NAME


def test_plugin_registers_workflow_and_activities_with_worker():
    # The plugin must inject TaskTrackerWorkflow + activities so a Worker needs no explicit lists.
    # run_context (which opens the FastMCP session) only runs on worker.run(), not at construction.
    async def _build() -> None:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            worker = Worker(
                env.client,
                task_queue=models.TASK_QUEUE,
                plugins=[MCPTasksClientPlugin(_CONFIG, env.client)],
                workflow_runner=_RUNNER,
            )
            assert worker is not None

    asyncio.run(_build())
