# ABOUTME: Client-side Temporal worker for the MCP tasks extension — adopts the durable client plugin.
# One MCPTasksClientPlugin registers TaskTrackerWorkflow + wire activities and owns the MCP session.

from __future__ import annotations

import asyncio
import json

import click
from temporalio.client import Client as TemporalClient
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from invoice_processing_mcp.client import backoffice_activities
from invoice_processing_mcp.client.purchase_order_workflow import PurchaseOrderWorkflow
from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.plugin import MCPTasksClientPlugin

# beartype installs import hooks that break inside the workflow sandbox; pass it through.
_SANDBOX_RESTRICTIONS = SandboxRestrictions.default.with_passthrough_modules("beartype")


def _load_config(config_path: str) -> dict:
    """Load the Claude Desktop-format MCP config (FastMCP consumes it directly)."""
    with open(config_path) as f:
        config: dict = json.load(f)
    return config


async def run_worker(config_path: str, temporal_address: str) -> None:
    config = _load_config(config_path)
    client = await TemporalClient.connect(temporal_address)
    worker = Worker(
        client,
        task_queue=models.TASK_QUEUE,
        # PurchaseOrderWorkflow + back-office activities are MERGED with the plugin's
        # TaskTrackerWorkflow + MCP activities (do NOT re-list TaskTrackerWorkflow here).
        workflows=[PurchaseOrderWorkflow],
        activities=[
            backoffice_activities.record_goods_receipt,
            backoffice_activities.update_inventory,
            backoffice_activities.notify_requester,
            backoffice_activities.close_po,
        ],
        plugins=[MCPTasksClientPlugin(config, client)],
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
