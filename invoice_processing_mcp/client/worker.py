# ABOUTME: Client-side Temporal worker for the MCP tasks extension — adopts the durable client plugin.
# One MCPTasksClientPlugin registers TaskTrackerWorkflow + wire activities and owns the MCP session.

from __future__ import annotations

import asyncio
import json

import click
from mcp.client.stdio import StdioServerParameters
from temporalio.client import Client as TemporalClient
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.plugin import MCPTasksClientPlugin

# beartype installs import hooks that break inside the workflow sandbox; pass it through.
_SANDBOX_RESTRICTIONS = SandboxRestrictions.default.with_passthrough_modules("beartype")


def _server_params(config_path: str) -> StdioServerParameters:
    """Build StdioServerParameters from a Claude Desktop-format MCP config (first server)."""
    with open(config_path) as f:
        config = json.load(f)
    servers = config["mcpServers"]
    _, entry = next(iter(servers.items()))
    return StdioServerParameters(
        command=entry["command"],
        args=entry.get("args", []),
        env=entry.get("env") or None,
    )


async def run_worker(config_path: str, temporal_address: str) -> None:
    server_params = _server_params(config_path)
    client = await TemporalClient.connect(temporal_address)
    worker = Worker(
        client,
        task_queue=models.TASK_QUEUE,
        plugins=[MCPTasksClientPlugin(server_params)],
        workflow_runner=SandboxedWorkflowRunner(restrictions=_SANDBOX_RESTRICTIONS),
    )
    click.echo(f"Worker started on queue '{models.TASK_QUEUE}'. Press Ctrl+C to stop.")
    await worker.run()


@click.command()
@click.option(
    "--config",
    default="invoice_processing_mcp/client_config.json",
    help="MCP server config file (Claude Desktop format)",
)
@click.option(
    "--temporal-address",
    envvar="TEMPORAL_ADDRESS",
    default="localhost:7233",
    help="Temporal server address",
)
def main(config: str, temporal_address: str) -> None:
    """Start the client-side Temporal worker (durable MCP tasks client)."""
    asyncio.run(run_worker(config, temporal_address))


if __name__ == "__main__":
    main()
