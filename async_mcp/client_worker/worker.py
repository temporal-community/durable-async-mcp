# ABOUTME: Temporal worker startup for the client-side MCP task tracker.
# Runs TaskTrackerWorkflow and MCPActivities against the client-task-queue.

from __future__ import annotations

import asyncio
import json
import os

import click
from fastmcp import Client as FastMCPClient
from temporalio.client import Client as TemporalClient
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from async_mcp.client_worker.activities import MCPActivities
from async_mcp.client_worker.workflows import CLIENT_TASK_QUEUE, TaskTrackerWorkflow

# beartype installs import hooks that cause circular imports inside Temporal's
# workflow sandbox. Pass it through so the sandbox never tries to sandbox-import it.
_SANDBOX_RESTRICTIONS = SandboxRestrictions.default.with_passthrough_modules("beartype")


def _load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


async def run_worker(config_path: str, temporal_address: str) -> None:
    """Connect to Temporal and MCP, then start the worker."""
    config = _load_config(config_path)

    temporal_client = await TemporalClient.connect(temporal_address)

    # Create activities first so the elicitation handler is available as a
    # bound method before the MCP client is constructed.
    acts = MCPActivities(mcp_client=None, temporal_client=temporal_client)

    async with FastMCPClient(config, elicitation_handler=acts._elicitation_handler) as mcp:
        acts._mcp = mcp

        worker = Worker(
            temporal_client,
            task_queue=CLIENT_TASK_QUEUE,
            workflows=[TaskTrackerWorkflow],
            activities=[
                acts.start_task,
                acts.poll_task_status,
                acts.handle_elicitation,
                acts.get_task_result,
            ],
            workflow_runner=SandboxedWorkflowRunner(restrictions=_SANDBOX_RESTRICTIONS),
        )
        click.echo(f"Worker started on queue '{CLIENT_TASK_QUEUE}'. Press Ctrl+C to stop.")
        await worker.run()


@click.command()
@click.option(
    "--config",
    default="async_mcp/client_config.json",
    help="MCP server config file (Claude Desktop format)",
)
@click.option(
    "--temporal-address",
    envvar="TEMPORAL_ADDRESS",
    default="localhost:7233",
    help="Temporal server address",
)
def main(config: str, temporal_address: str) -> None:
    """Start the client-side Temporal worker."""
    asyncio.run(run_worker(config, temporal_address))


if __name__ == "__main__":
    main()
