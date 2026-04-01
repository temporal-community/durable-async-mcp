# ABOUTME: MCP server exposing invoice processing as individual synchronous tools.
# Uses Temporal workflows for durable execution, designed for Claude Desktop over stdio.

import os
import uuid
from typing import Dict, List

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from temporalio.client import Client

from bizservice.workflows import InvoiceWorkflow


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


async def _client() -> Client:
    return await Client.connect(os.getenv("TEMPORAL_ADDRESS", "localhost:7233"))


mcp = FastMCP("invoice_processor")


@mcp.tool()
async def process_invoice(invoice: Invoice) -> Dict[str, str]:
    """Start invoice processing through validation, approval, and payment.

    Kicks off a Temporal workflow that validates the invoice against the ERP
    system, then waits for human approval before processing payments.

    Returns the workflow_id and run_id needed to check status or approve/reject.
    """
    invoice_dict = invoice.model_dump()
    client = await _client()
    workflow_id = f"invoice-{uuid.uuid4()}"
    handle = await client.start_workflow(
        InvoiceWorkflow.run,
        invoice_dict,
        id=workflow_id,
        task_queue="invoice-task-queue",
    )
    return {"workflow_id": handle.id, "run_id": handle.result_run_id}


@mcp.tool()
async def approve_invoice(workflow_id: str, run_id: str) -> str:
    """Signal approval for an invoice workflow that is awaiting approval.

    Use this after checking invoice_status shows PENDING-APPROVAL.
    """
    client = await _client()
    handle = client.get_workflow_handle(workflow_id=workflow_id, run_id=run_id)
    await handle.signal(InvoiceWorkflow.ApproveInvoice)
    return "APPROVED"


@mcp.tool()
async def reject_invoice(workflow_id: str, run_id: str) -> str:
    """Signal rejection for an invoice workflow that is awaiting approval.

    Use this after checking invoice_status shows PENDING-APPROVAL.
    """
    client = await _client()
    handle = client.get_workflow_handle(workflow_id=workflow_id, run_id=run_id)
    await handle.signal(InvoiceWorkflow.RejectInvoice)
    return "REJECTED"


@mcp.tool()
async def invoice_status(workflow_id: str, run_id: str) -> str:
    """Query the current status of an invoice workflow.

    Returns the invoice processing status (e.g. PENDING-VALIDATION,
    PENDING-APPROVAL, APPROVED, PAYING, PAID, FAILED, REJECTED)
    along with the Temporal workflow execution status.
    """
    client = await _client()
    handle = client.get_workflow_handle(workflow_id=workflow_id, run_id=run_id)
    desc = await handle.describe()
    status = await handle.query(InvoiceWorkflow.GetInvoiceStatus)
    return (
        f"Invoice with ID {workflow_id} is currently {status}. "
        f"Workflow status: {desc.status.name}"
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
