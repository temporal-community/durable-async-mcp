# ABOUTME: Tests InvoiceWorkflow's approval gate under the time-skipping env with stub activities.
# Small invoices (<= AUTO_APPROVE_THRESHOLD) pay with no human input; larger ones still wait for approval.

import asyncio
import uuid
from datetime import timedelta

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from bizservice.workflows import InvoiceWorkflow, PayLineItem

TASK_QUEUE = "test-invoice-queue"


@activity.defn(name="validate_against_erp")
async def stub_validate(invoice: dict) -> bool:
    return True


@activity.defn(name="reconcile_with_erp")
async def stub_reconcile(invoice: dict) -> dict:
    return {}


@activity.defn(name="payment_gateway")
async def stub_payment(line: dict) -> bool:
    return True


def _invoice(*amounts: float) -> dict:
    return {
        "invoice_id": "INV-T",
        "customer": "Test",
        "lines": [
            {"description": f"L{i}", "amount": a, "due_date": "2024-06-30T00:00:00Z"}
            for i, a in enumerate(amounts)
        ],
    }


def _worker(env: WorkflowEnvironment) -> Worker:
    return Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[InvoiceWorkflow, PayLineItem],
        activities=[stub_validate, stub_reconcile, stub_payment],
    )


async def test_invoice_at_threshold_is_paid_without_approval():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env):
            result = await env.client.execute_workflow(
                InvoiceWorkflow.run,
                _invoice(200, 300),
                id=f"inv-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
                # Well under the 5-day approval wait: fails fast if the gate is (wrongly) taken.
                execution_timeout=timedelta(days=1),
            )
    assert result == "PAID"


async def test_invoice_over_threshold_waits_for_approval():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env):
            handle = await env.client.start_workflow(
                InvoiceWorkflow.run,
                _invoice(200, 300.01),
                id=f"inv-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            # Poll in real time (queries don't advance the time-skipping clock).
            for _ in range(100):
                if await handle.query(InvoiceWorkflow.GetInvoiceStatus) == "PENDING-APPROVAL":
                    break
                await asyncio.sleep(0.05)
            assert await handle.query(InvoiceWorkflow.GetInvoiceStatus) == "PENDING-APPROVAL"

            await handle.signal(InvoiceWorkflow.ApproveInvoice)
            assert await handle.result() == "PAID"
