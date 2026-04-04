# ABOUTME: Temporal-backed MCP task protocol handlers that replace FastMCP's Docket/Redis layer.
# Maps MCP task lifecycle (get/result/list/cancel) directly to Temporal workflow operations.

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import mcp.types
from mcp.shared.exceptions import McpError
from mcp.types import (
    INVALID_PARAMS,
    CallToolResult,
    CancelTaskRequest,
    CancelTaskResult,
    ErrorData,
    GetTaskPayloadRequest,
    GetTaskRequest,
    GetTaskResult,
    ListTasksRequest,
    ListTasksResult,
    ServerResult,
    Task,
    TextContent,
)
from temporalio.client import Client, WorkflowExecutionStatus

from bizservice.workflows import InvoiceWorkflow

if TYPE_CHECKING:
    from fastmcp.server.server import FastMCP

logger = logging.getLogger(__name__)

# Temporal workflow status -> MCP task state
TEMPORAL_TO_MCP_STATE: dict[str, str] = {
    "INITIALIZING": "working",
    "PENDING-VALIDATION": "working",
    "PENDING-APPROVAL": "input_required",
    "APPROVED": "working",
    "PAYING": "working",
    "PAID": "completed",
    "FAILED": "failed",
    "REJECTED": "completed",
}

TERMINAL_STATUSES = {"PAID", "FAILED", "REJECTED"}

TASK_TTL_MS = 5 * 24 * 60 * 60 * 1000  # 5 days in milliseconds
POLL_INTERVAL_MS = 2000  # 2 seconds


async def _get_temporal_client() -> Client:
    """Get a cached Temporal client connection."""
    if not hasattr(_get_temporal_client, "_client"):
        _get_temporal_client._client = await Client.connect(
            os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
        )
    return _get_temporal_client._client


def reset_temporal_client() -> None:
    """Reset the cached client. Useful for testing."""
    if hasattr(_get_temporal_client, "_client"):
        del _get_temporal_client._client


async def _query_workflow_status(workflow_id: str) -> tuple[str, datetime]:
    """Query a Temporal workflow for its invoice status and start time.

    Returns:
        Tuple of (invoice_status, start_time)

    Raises:
        McpError if workflow not found
    """
    client = await _get_temporal_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        desc = await handle.describe()
        status = await handle.query(InvoiceWorkflow.GetInvoiceStatus)
        return status, desc.start_time
    except Exception as e:
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message=f"Task {workflow_id} not found")
        ) from e


async def handle_tasks_get(req: GetTaskRequest) -> ServerResult:
    """Handle MCP tasks/get — query Temporal workflow status."""
    task_id = req.params.taskId
    if not task_id:
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message="Missing required parameter: taskId")
        )

    invoice_status, start_time = await _query_workflow_status(task_id)
    mcp_state = TEMPORAL_TO_MCP_STATE.get(invoice_status, "failed")

    status_message = f"Invoice status: {invoice_status}"

    return ServerResult(
        GetTaskResult(
            taskId=task_id,
            status=mcp_state,
            createdAt=start_time,
            lastUpdatedAt=datetime.now(timezone.utc),
            ttl=TASK_TTL_MS,
            pollInterval=POLL_INTERVAL_MS,
            statusMessage=status_message,
        )
    )


async def handle_tasks_result(
    req: GetTaskPayloadRequest, server: FastMCP
) -> ServerResult:
    """Handle MCP tasks/result — return final result or trigger elicitation.

    For terminal workflows: returns CallToolResult with the outcome.
    For PENDING-APPROVAL: triggers elicitation, signals workflow, awaits completion.
    For working states: raises error (task not ready).
    """
    import fastmcp.server.context

    task_id = req.params.taskId
    if not task_id:
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message="Missing required parameter: taskId")
        )

    client = await _get_temporal_client()
    handle = client.get_workflow_handle(task_id)

    try:
        invoice_status = await handle.query(InvoiceWorkflow.GetInvoiceStatus)
    except Exception as e:
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message=f"Task {task_id} not found")
        ) from e

    # Terminal states — return the result
    if invoice_status in TERMINAL_STATUSES:
        return ServerResult(
            _build_terminal_result(task_id, invoice_status)
        )

    # PENDING-APPROVAL — run elicitation flow
    if invoice_status == "PENDING-APPROVAL":
        invoice_data = await handle.query(InvoiceWorkflow.GetInvoiceData)
        customer = invoice_data.get("customer", "unknown")
        invoice_id = invoice_data.get("invoice_id", "unknown")
        total_amount = sum(
            line.get("amount", 0) for line in invoice_data.get("lines", [])
        )
        line_count = len(invoice_data.get("lines", []))

        async with fastmcp.server.context.Context(fastmcp=server) as ctx:
            elicit_response = await ctx.elicit(
                message=(
                    f"Invoice {invoice_id} for {customer} "
                    f"(${total_amount:.2f}) requires approval. "
                    f"Lines: {line_count} items.\n\n"
                    f"Select 'approve' or 'reject':"
                ),
                response_type=["approve", "reject"],
            )

        # Signal the workflow, then block until terminal per MCP spec
        # (Result Retrieval #3: tasks/result MUST block until terminal).
        # The client cancels this request after elicitation and resumes
        # polling — the response from handle.result() goes nowhere, but
        # the server remains spec-conformant.
        if elicit_response.action in ("cancel", "decline"):
            await handle.signal(InvoiceWorkflow.RejectInvoice)
            result = await handle.result()
            return ServerResult(
                _build_terminal_result(task_id, result)
            )

        decision = elicit_response.data.lower() if elicit_response.data else "reject"
        if decision == "reject":
            await handle.signal(InvoiceWorkflow.RejectInvoice)
        else:
            await handle.signal(InvoiceWorkflow.ApproveInvoice)

        result = await handle.result()
        return ServerResult(
            _build_terminal_result(task_id, result)
        )

    # Still working — not ready for result retrieval
    mcp_state = TEMPORAL_TO_MCP_STATE.get(invoice_status, "working")
    raise McpError(
        ErrorData(
            code=INVALID_PARAMS,
            message=f"Task not completed yet (current state: {mcp_state})",
        )
    )


def _build_terminal_result(task_id: str, status: str) -> CallToolResult:
    """Build a CallToolResult for a terminal workflow state."""
    is_error = status == "FAILED"
    return CallToolResult(
        content=[TextContent(type="text", text=f"Invoice processing result: {status}")],
        isError=is_error,
        _meta={
            "modelcontextprotocol.io/related-task": {
                "taskId": task_id,
            }
        },
    )


async def handle_tasks_list(req: ListTasksRequest) -> ServerResult:
    """Handle MCP tasks/list — list active invoice workflows from Temporal."""
    client = await _get_temporal_client()
    tasks: list[Task] = []

    async for workflow in client.list_workflows('WorkflowType = "InvoiceWorkflow"'):
        # Only include running workflows
        if workflow.status != WorkflowExecutionStatus.RUNNING:
            continue

        workflow_id = workflow.id
        try:
            handle = client.get_workflow_handle(workflow_id)
            invoice_status = await handle.query(InvoiceWorkflow.GetInvoiceStatus)
            mcp_state = TEMPORAL_TO_MCP_STATE.get(invoice_status, "working")
        except Exception:
            mcp_state = "working"

        tasks.append(
            Task(
                taskId=workflow_id,
                status=mcp_state,
                statusMessage=f"Invoice workflow {workflow_id}",
                createdAt=workflow.start_time,
                lastUpdatedAt=datetime.now(timezone.utc),
                ttl=TASK_TTL_MS,
                pollInterval=POLL_INTERVAL_MS,
            )
        )

    return ServerResult(ListTasksResult(tasks=tasks, nextCursor=None))


async def handle_tasks_cancel(req: CancelTaskRequest) -> ServerResult:
    """Handle MCP tasks/cancel — cancel a Temporal workflow."""
    task_id = req.params.taskId
    if not task_id:
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message="Missing required parameter: taskId")
        )

    client = await _get_temporal_client()
    handle = client.get_workflow_handle(task_id)

    try:
        desc = await handle.describe()
    except Exception as e:
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message=f"Task {task_id} not found")
        ) from e

    # Check if already terminal
    if desc.status in (
        WorkflowExecutionStatus.COMPLETED,
        WorkflowExecutionStatus.FAILED,
        WorkflowExecutionStatus.CANCELED,
        WorkflowExecutionStatus.TERMINATED,
    ):
        raise McpError(
            ErrorData(
                code=INVALID_PARAMS,
                message="Cannot cancel task: already in terminal status",
            )
        )

    await handle.cancel()

    return ServerResult(
        CancelTaskResult(
            taskId=task_id,
            status="cancelled",
            createdAt=desc.start_time,
            lastUpdatedAt=datetime.now(timezone.utc),
            ttl=TASK_TTL_MS,
            pollInterval=POLL_INTERVAL_MS,
            statusMessage="Task cancelled",
        )
    )


def _make_wrapped_call_tool(original_handler: Any, server: FastMCP) -> Any:
    """Create a wrapped CallToolRequest handler that intercepts task-augmented process_invoice calls.

    For process_invoice with task metadata: starts the Temporal workflow and
    returns task metadata immediately.
    For everything else: delegates to FastMCP's original handler.
    """

    async def handler(req: mcp.types.CallToolRequest) -> ServerResult:
        tool_name = req.params.name

        # Only intercept process_invoice
        if tool_name != "process_invoice":
            return await original_handler(req)

        # Check for task metadata via request context
        try:
            ctx = server._mcp_server.request_context
            is_task = ctx.experimental.is_task
        except (AttributeError, LookupError):
            is_task = False

        if not is_task:
            # Synchronous call — delegate to FastMCP's normal handler
            return await original_handler(req)

        # Task-augmented call — start the Temporal workflow and return task stub
        arguments = req.params.arguments or {}
        invoice = arguments.get("invoice", arguments)

        client = await _get_temporal_client()
        workflow_id = f"invoice-{uuid.uuid4()}"
        await client.start_workflow(
            InvoiceWorkflow.run,
            invoice,
            id=workflow_id,
            task_queue="invoice-task-queue",
        )

        logger.info("Started workflow %s for task-augmented process_invoice", workflow_id)

        return ServerResult(
            CallToolResult(
                content=[],
                _meta={
                    "modelcontextprotocol.io/task": {
                        "taskId": workflow_id,
                        "status": "working",
                    }
                },
            )
        )

    return handler


def register_temporal_task_handlers(mcp_server: FastMCP) -> None:
    """Replace FastMCP's Docket-based task handlers with Temporal-backed ones.

    Overwrites all 5 task-related request handlers on the low-level MCP server:
    - CallToolRequest (wrapped to intercept task-augmented process_invoice)
    - GetTaskRequest (tasks/get)
    - GetTaskPayloadRequest (tasks/result)
    - ListTasksRequest (tasks/list)
    - CancelTaskRequest (tasks/cancel)
    """
    low_level = mcp_server._mcp_server

    # Save and wrap the original CallToolRequest handler
    original_call_tool = low_level.request_handlers[mcp.types.CallToolRequest]
    low_level.request_handlers[mcp.types.CallToolRequest] = _make_wrapped_call_tool(
        original_call_tool, mcp_server
    )

    # tasks/get
    low_level.request_handlers[GetTaskRequest] = handle_tasks_get

    # tasks/result — needs server reference for elicitation context
    async def _handle_tasks_result(req: GetTaskPayloadRequest) -> ServerResult:
        return await handle_tasks_result(req, mcp_server)

    low_level.request_handlers[GetTaskPayloadRequest] = _handle_tasks_result

    # tasks/list
    low_level.request_handlers[ListTasksRequest] = handle_tasks_list

    # tasks/cancel
    low_level.request_handlers[CancelTaskRequest] = handle_tasks_cancel

    logger.info("Registered Temporal-backed MCP task handlers")
