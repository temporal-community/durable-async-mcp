# ABOUTME: Tests PurchaseOrderWorkflow under the time-skipping env — parent runs back-office work
# concurrently with the child TaskTrackerWorkflow (MCP activities faked), then completes on payment.

import asyncio
import os
import uuid

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from invoice_processing_mcp.client import backoffice_activities
from invoice_processing_mcp.client.purchase_order_workflow import PurchaseOrderWorkflow
from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.client.workflows import TaskTrackerWorkflow

ORDER = {
    "po_id": "PO-1",
    "requester": "alice",
    "invoice": {"invoice_id": "INV-1", "customer": "Acme", "lines": []},
}


class FakeMCPActivities:
    """Stubs the MCP wire activities using the server-push model. handle_elicitation surfaces the
    inputRequests (via the elicitation_received signal) and blocks until the human decision arrives,
    mirroring the real activity; poll then reports completed once a gate has been answered.
    """

    def __init__(self) -> None:
        self.client = None  # set to env.client before the worker runs
        self.elicited = False

    @activity.defn(name=models.START_TASK)
    async def start_task(self, inp: models.StartTaskInput) -> str:
        return "task-1"

    @activity.defn(name=models.POLL_TASK)
    async def poll_task(self, task_id: str) -> str:
        return "completed" if self.elicited else "input_required"

    @activity.defn(name=models.HANDLE_ELICITATION)
    async def handle_elicitation(self, task_id: str) -> str:
        wf_id = activity.info().workflow_id
        handle = self.client.get_workflow_handle(wf_id)
        await handle.signal(
            "elicitation_received",
            {
                "approval": {
                    "method": "elicitation/create",
                    "params": {"message": "Approve?", "requestedSchema": {}},
                }
            },
        )
        for _ in range(200):
            if await handle.query("get_pending_decision"):
                self.elicited = True
                return "elicitation_handled"
            await asyncio.sleep(0.05)
        raise RuntimeError("no decision received")

    @activity.defn(name=models.GET_TASK_RESULT)
    async def get_task_result(self, task_id: str) -> dict:
        return {"content": [{"type": "text", "text": "PAID"}], "isError": False}


async def _scenario() -> None:
    os.environ["BACKOFFICE_DELAY_SECONDS"] = "0"
    fake = FakeMCPActivities()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        fake.client = env.client
        async with Worker(
            env.client,
            task_queue="po-test",
            workflows=[PurchaseOrderWorkflow, TaskTrackerWorkflow],
            activities=[
                backoffice_activities.record_goods_receipt,
                backoffice_activities.update_inventory,
                backoffice_activities.notify_requester,
                backoffice_activities.close_po,
                fake.start_task,
                fake.poll_task,
                fake.handle_elicitation,
                fake.get_task_result,
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "beartype"
                )
            ),
        ):
            po = await env.client.start_workflow(
                PurchaseOrderWorkflow.run,
                ORDER,
                id=f"po-{uuid.uuid4()}",
                task_queue="po-test",
            )

            # Back-office work should finish WHILE the payment child is still awaiting input.
            progress = {}
            for _ in range(100):
                progress = await po.query(PurchaseOrderWorkflow.get_progress)
                if "po_closed" in progress["steps_done"]:
                    break
                await asyncio.sleep(0.05)
            assert "po_closed" in progress["steps_done"]
            assert (
                progress["payment_status"] == "working"
            )  # payment still pending → concurrency

            # The child tracker id is surfaced by the parent; answer its elicitation.
            child_id = progress["payment_workflow_id"]
            assert child_id is not None
            child = env.client.get_workflow_handle(child_id)
            await child.signal(
                TaskTrackerWorkflow.user_decision,
                {"approval": {"action": "accept", "content": {"value": "approve"}}},
            )

            result = await po.result()

    assert result["fulfilled"] is True
    assert result["payment"]["status"] == "completed"
    assert result["po_id"] == "PO-1"


def test_purchase_order_runs_backoffice_concurrently():
    asyncio.run(_scenario())
