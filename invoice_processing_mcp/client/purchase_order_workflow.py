# ABOUTME: PurchaseOrderWorkflow — the client-side business process that consumes the invoice MCP server.
# Runs the process_invoice MCP task as a child TaskTrackerWorkflow while doing back-office work concurrently.

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow

from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.client.workflows import TaskTrackerWorkflow

with workflow.unsafe.imports_passed_through():
    from invoice_processing_mcp.client.backoffice_activities import (
        close_po,
        notify_requester,
        record_goods_receipt,
        update_inventory,
    )

_ACTIVITY_TIMEOUT = timedelta(seconds=30)
INVOICE_TOOL = "process_invoice"


@workflow.defn
class PurchaseOrderWorkflow:
    """Fulfills a purchase order: pays the supplier invoice via an MCP task while concurrently
    running the back-office steps. The MCP task is a child TaskTrackerWorkflow; this workflow does
    other durable work in parallel and only finishes once both the payment and the back-office
    steps are complete."""

    def __init__(self) -> None:
        self._steps_done: list[str] = []
        self._payment_status: str = "working"
        self._payment_workflow_id: str | None = None

    @workflow.run
    async def run(self, order: dict) -> dict[str, Any]:
        # 1. Goods receipt kicks off fulfillment before we involve payment.
        await workflow.execute_activity(
            record_goods_receipt, order, start_to_close_timeout=_ACTIVITY_TIMEOUT
        )
        self._steps_done.append("goods_receipt")

        # 2. Start the supplier-invoice payment as an MCP task (child) — don't await it yet.
        self._payment_workflow_id = f"task-tracker-{workflow.uuid4()}"
        payment = await workflow.start_child_workflow(
            TaskTrackerWorkflow.run,
            models.TaskTrackerInput(
                tool_name=INVOICE_TOOL, arguments={"invoice": order["invoice"]}
            ),
            id=self._payment_workflow_id,
        )

        # 3. Do the rest of the back-office work CONCURRENTLY while payment is pending/in-HITL.
        payment_result, _ = await asyncio.gather(payment, self._run_backoffice(order))
        self._payment_status = payment_result.get("status", "unknown")

        return {
            "po_id": order.get("po_id"),
            "payment": payment_result,
            "fulfilled": payment_result.get("status") == "completed",
        }

    async def _run_backoffice(self, order: dict) -> None:
        for step_name, act in (
            ("inventory", update_inventory),
            ("requester_notified", notify_requester),
            ("po_closed", close_po),
        ):
            await workflow.execute_activity(
                act, order, start_to_close_timeout=_ACTIVITY_TIMEOUT
            )
            self._steps_done.append(step_name)

    @workflow.query
    def get_progress(self) -> dict[str, Any]:
        """Back-office steps completed so far + last-known payment status (for the UI)."""
        return {
            "steps_done": self._steps_done,
            "payment_status": self._payment_status,
            "payment_workflow_id": self._payment_workflow_id,
        }
