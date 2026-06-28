# ABOUTME: MCP server exposing invoice processing as a task via the Temporal-backed tasks protocol.
# Built on FastMCP; process_invoice (task=required) creates an InvoiceWorkflow-backed task.

from __future__ import annotations

import asyncio
import os
from typing import List

from fastmcp import FastMCP
from fastmcp.server.tasks import TaskConfig
from pydantic import BaseModel, Field
from temporalio.client import Client

from invoice_processing_mcp.server.invoice_backend import (
    INVOICE_TOOL,
    InvoiceTaskBackend,
)
from mcp_tasks_temporal.server import register_tasks_extension


class LineItem(BaseModel):
    description: str = Field(description="Description of the line item")
    amount: float = Field(description="Amount in dollars")
    due_date: str = Field(
        description="Payment due date in ISO 8601 format (e.g. 2024-06-30T00:00:00Z)"
    )


class Invoice(BaseModel):
    invoice_id: str = Field(description="Unique invoice identifier")
    customer: str = Field(description="Customer name")
    lines: List[LineItem] = Field(description="Line items to be paid")


def build_server(client: Client) -> FastMCP:
    """Build the invoice MCP server with the tasks protocol wired to Temporal."""
    mcp = FastMCP("invoice_processor")

    @mcp.tool(task=TaskConfig(mode="required"))
    async def process_invoice(invoice: Invoice) -> dict:
        """Process an invoice through validation, human approval, and payment.

        Long-running: returns a task immediately. Poll tasks/get; when input_required, answer
        the approval (and, for large invoices, cost-center) elicitation pushed during tasks/result.
        """
        # The tasks layer intercepts task-augmented calls and starts the workflow via the backend;
        # this body is not executed for task calls.
        return {"invoice_id": invoice.invoice_id}

    register_tasks_extension(mcp, InvoiceTaskBackend(client), task_tools={INVOICE_TOOL})
    return mcp


async def main() -> None:
    client = await Client.connect(os.getenv("TEMPORAL_ADDRESS", "localhost:7233"))
    mcp = build_server(client)
    await mcp.run_async(transport="stdio")


if __name__ == "__main__":
    asyncio.run(main())
