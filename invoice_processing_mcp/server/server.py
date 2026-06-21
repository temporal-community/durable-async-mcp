# ABOUTME: MCP server exposing invoice processing as a task via the Temporal-backed tasks extension.
# Built on the v2 lowlevel Server; process_invoice creates an InvoiceWorkflow-backed task.

from __future__ import annotations

import asyncio
import os
from typing import Any

from mcp import types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from temporalio.client import Client

from invoice_processing_mcp.server.invoice_backend import (
    INVOICE_TOOL,
    InvoiceTaskBackend,
)
from mcp_tasks_temporal.server import register_tasks_extension

_LINE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "Description of the line item",
        },
        "amount": {"type": "number", "description": "Amount in dollars"},
        "due_date": {"type": "string", "description": "Payment due date (ISO 8601)"},
    },
    "required": ["description", "amount", "due_date"],
}

INVOICE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice": {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "string",
                    "description": "Unique invoice identifier",
                },
                "customer": {"type": "string", "description": "Customer name"},
                "lines": {
                    "type": "array",
                    "items": _LINE_ITEM_SCHEMA,
                    "description": "Line items to pay",
                },
            },
            "required": ["invoice_id", "customer", "lines"],
        }
    },
    "required": ["invoice"],
}

_PROCESS_INVOICE_DESCRIPTION = (
    "Process an invoice through validation, human approval, and payment. "
    "Long-running: returns a task immediately. Poll tasks/get; when input_required, "
    "answer the approval elicitation via tasks/update."
)


async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name=INVOICE_TOOL,
                description=_PROCESS_INVOICE_DESCRIPTION,
                inputSchema=INVOICE_TOOL_SCHEMA,
            )
        ]
    )


def build_server(client: Client) -> Server:
    """Build the invoice MCP server with the tasks extension wired to Temporal."""
    server = Server("invoice_processor", on_list_tools=on_list_tools)
    register_tasks_extension(
        server, InvoiceTaskBackend(client), task_tools={INVOICE_TOOL}
    )
    return server


async def main() -> None:
    client = await Client.connect(os.getenv("TEMPORAL_ADDRESS", "localhost:7233"))
    server = build_server(client)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
