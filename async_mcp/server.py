# ABOUTME: MCP server exposing invoice processing as a task-enabled tool.
# Uses Temporal workflows for durable execution with custom task handlers replacing Docket/Redis.

import os
import uuid
from typing import List

from fastmcp import FastMCP
from fastmcp.server.tasks import TaskConfig
from pydantic import BaseModel, Field
from temporalio.client import Client

from bizservice.workflows import InvoiceWorkflow

from async_mcp.temporal_task_handlers import register_temporal_task_handlers


class LineItem(BaseModel):
    description: str = Field(description="Description of the line item")
    amount: float = Field(description="Amount in dollars")
    due_date: str = Field(description="Payment due date in ISO 8601 format (e.g. 2024-06-30T00:00:00Z)")


class Invoice(BaseModel):
    invoice_id: str = Field(description="Unique invoice identifier")
    customer: str = Field(description="Customer name")
    lines: List[LineItem] = Field(description="Line items to be paid")


async def _client() -> Client:
    return await Client.connect(os.getenv("TEMPORAL_ADDRESS", "localhost:7233"))


mcp = FastMCP("invoice_processor")

# Replace Docket-based task handlers with Temporal-backed ones
register_temporal_task_handlers(mcp)


@mcp.tool(task=TaskConfig(mode="required"))
async def process_invoice(invoice: Invoice) -> dict:
    """Process an invoice through validation, approval, and payment.

    This is a long-running task that:
    1. Validates the invoice against the ERP system
    2. Waits for human approval (via elicitation)
    3. Processes payments for each line item

    The task will transition to 'input_required' state when awaiting approval.

    When called with task metadata, returns immediately with a task ID.
    Poll tasks/get for status; call tasks/result when input_required or completed.
    """
    invoice_dict = invoice.model_dump()
    client = await _client()
    workflow_id = f"invoice-{uuid.uuid4()}"
    await client.start_workflow(
        InvoiceWorkflow.run,
        invoice_dict,
        id=workflow_id,
        task_queue="invoice-task-queue",
    )
    return {
        "workflow_id": workflow_id,
        "invoice_id": invoice.invoice_id,
    }

if __name__ == "__main__":
    mcp.run(transport="stdio")
