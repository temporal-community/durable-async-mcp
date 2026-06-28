# ABOUTME: InvoiceTaskBackend — the Temporal-backed TaskBackend for the invoice app (task ID = workflow ID).
# Maps InvoiceWorkflow status to MCP task state, builds the approval elicitation, and applies decisions.

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from mcp.shared.exceptions import MCPError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS
from temporalio.client import Client

from bizservice.workflows import InvoiceWorkflow
from mcp_tasks_temporal.backend import TaskState
from mcp_tasks_temporal.wire import InputRequest, InputResponse

INVOICE_TOOL = "process_invoice"
INVOICE_TASK_QUEUE = "invoice-task-queue"
APPROVAL_KEY = "approval"
COST_CENTER_KEY = "cost-center-coding"

# The workflow-state -> task-state map: InvoiceWorkflow's domain status -> MCP task status.
# This dict (plus the per-status payload built in get_state) is the developer's mapping job.
TEMPORAL_TO_MCP_STATE: dict[str, str] = {
    "INITIALIZING": "working",
    "PENDING-VALIDATION": "working",
    "PENDING-APPROVAL": "input_required",
    "APPROVED": "working",
    "RECONCILING": "working",
    "PENDING-COST-CENTER": "input_required",
    "CODED": "working",
    "PAYING": "working",
    "PAID": "completed",
    "FAILED": "failed",
    "REJECTED": "completed",
}

TASK_TTL_MS = 5 * 24 * 60 * 60 * 1000  # 5 days
POLL_INTERVAL_MS = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _approval_request(invoice: dict[str, Any]) -> InputRequest:
    customer = invoice.get("customer", "unknown")
    invoice_id = invoice.get("invoice_id", "unknown")
    lines = invoice.get("lines", [])
    total = sum(line.get("amount", 0) for line in lines)
    return InputRequest(
        method="elicitation/create",
        params={
            "mode": "form",
            "message": (
                f"Invoice {invoice_id} for {customer} (${total:.2f}) requires approval. "
                f"Lines: {len(lines)} items.\n\nSelect 'approve' or 'reject':"
            ),
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "enum": ["approve", "reject"]}
                },
                "required": ["value"],
            },
        },
    )


def _cost_center_request(invoice: dict[str, Any]) -> InputRequest:
    invoice_id = invoice.get("invoice_id", "unknown")
    total = sum(line.get("amount", 0) for line in invoice.get("lines", []))
    return InputRequest(
        method="elicitation/create",
        params={
            "mode": "form",
            "message": (
                f"Invoice {invoice_id} (${total:.2f}) exceeds the auto-approval limit. "
                f"Assign a cost center to release payment:"
            ),
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "cost_center": {
                        "type": "string",
                        "description": "GL cost-center code (e.g. CC-1000)",
                    },
                    "memo": {
                        "type": "string",
                        "description": "Optional note for the audit trail",
                    },
                },
                "required": ["cost_center"],
            },
        },
    )


def _terminal_result(invoice_status: str) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": f"Invoice processing result: {invoice_status}"}
        ],
        "isError": False,
        "resultType": "complete",
    }


def _response_action(response: Any) -> str | None:
    return (
        getattr(response, "action", None)
        if not isinstance(response, dict)
        else response.get("action")
    )


def _response_content(response: Any) -> dict[str, Any]:
    content = (
        getattr(response, "content", None)
        if not isinstance(response, dict)
        else response.get("content")
    )
    return content or {}


def _decision_is_approve(response: Any) -> bool:
    """Interpret an InputResponse (or its dict form) as approve/reject."""
    if _response_action(response) in ("decline", "cancel"):
        return False
    value = _response_content(response).get("value", "reject")
    return str(value).lower() == "approve"


class InvoiceTaskBackend:
    """Backs the tasks extension with Temporal: the MCP task ID is the InvoiceWorkflow ID.

    Its job is to map between two state models: the InvoiceWorkflow's domain *workflow state*
    (PENDING-APPROVAL, PAYING, PAID, ...) and the MCP *task state* (working, input_required,
    completed, ...). See TEMPORAL_TO_MCP_STATE and get_state().
    """

    def __init__(self, client: Client, *, task_queue: str = INVOICE_TASK_QUEUE) -> None:
        self._client = client
        self._task_queue = task_queue

    async def start(self, tool_name: str, arguments: dict[str, Any]) -> TaskState:
        invoice = arguments.get("invoice", arguments)
        workflow_id = f"invoice-{uuid.uuid4()}"
        await self._client.start_workflow(
            InvoiceWorkflow.run,
            invoice,
            id=workflow_id,
            task_queue=self._task_queue,
        )
        now = _now()
        return TaskState(
            task_id=workflow_id,
            status="working",
            created_at=now,
            last_updated_at=now,
            status_message="Invoice processing started",
            ttl_ms=TASK_TTL_MS,
            poll_interval_ms=POLL_INTERVAL_MS,
        )

    async def get_state(self, task_id: str) -> TaskState:
        # Map InvoiceWorkflow (workflow) state -> MCP TaskState.
        handle = self._client.get_workflow_handle(task_id)
        try:
            desc = await handle.describe()
            invoice_status = await handle.query(InvoiceWorkflow.GetInvoiceStatus)
        except Exception as e:
            raise MCPError(INVALID_PARAMS, f"Task {task_id} not found") from e

        mcp_state = TEMPORAL_TO_MCP_STATE.get(invoice_status, "failed")
        created = (
            desc.start_time.isoformat() if getattr(desc, "start_time", None) else _now()
        )
        state = TaskState(
            task_id=task_id,
            status=mcp_state,
            created_at=created,
            last_updated_at=_now(),
            status_message=f"Invoice status: {invoice_status}",
            ttl_ms=TASK_TTL_MS,
            poll_interval_ms=POLL_INTERVAL_MS,
        )

        if mcp_state == "input_required":
            invoice = await handle.query(InvoiceWorkflow.GetInvoiceData)
            if invoice_status == "PENDING-COST-CENTER":
                state.input_requests = {COST_CENTER_KEY: _cost_center_request(invoice)}
            else:
                state.input_requests = {APPROVAL_KEY: _approval_request(invoice)}
        elif invoice_status in ("PAID", "REJECTED"):
            state.result = _terminal_result(invoice_status)
        elif invoice_status == "FAILED":
            state.error = {
                "code": INTERNAL_ERROR,
                "message": "Invoice processing failed",
            }
        return state

    async def submit_input(
        self, task_id: str, input_responses: dict[str, InputResponse]
    ) -> None:
        handle = self._client.get_workflow_handle(task_id)

        # Second gate: cost-center coding. Decline/cancel rejects; otherwise record the coding.
        if COST_CENTER_KEY in input_responses:
            cc_response = input_responses[COST_CENTER_KEY]
            if _response_action(cc_response) in ("decline", "cancel"):
                await handle.signal(InvoiceWorkflow.RejectInvoice)
            else:
                await handle.signal(
                    InvoiceWorkflow.SubmitCostCenter, _response_content(cc_response)
                )
            return

        response = input_responses.get(APPROVAL_KEY)
        if _decision_is_approve(response):
            await handle.signal(InvoiceWorkflow.ApproveInvoice)
        else:
            await handle.signal(InvoiceWorkflow.RejectInvoice)

    async def cancel(self, task_id: str) -> None:
        await self._client.get_workflow_handle(task_id).cancel()
