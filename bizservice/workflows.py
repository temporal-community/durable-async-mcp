# ABOUTME: Temporal workflow definitions for invoice processing.
# Contains InvoiceWorkflow (main orchestrator with approval signals/queries) and PayLineItem (child workflow).

from __future__ import annotations

from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from bizservice.activities import validate_against_erp, payment_gateway


def _parse_due_date(due: str) -> datetime:
    if due.endswith("Z"):
        due = due[:-1] + "+00:00"
    return datetime.fromisoformat(due)


@workflow.defn
class PayLineItem:
    def __init__(self) -> None:
        self.force_pay: bool = False

    @workflow.signal
    async def ForcePayment(self) -> None:
        self.force_pay = True

    @workflow.run
    async def run(self, line: dict) -> str:
        due = _parse_due_date(line["due_date"])
        delay = (due - workflow.now()).total_seconds()
        if delay > 0:
            try:
                await workflow.wait_condition(
                    lambda: self.force_pay, timeout=timedelta(seconds=delay)
                )
                workflow.logger.info("Force payment signal received, skipping wait")
            except TimeoutError:
                pass  # Timer expired naturally, proceed to payment
        try:
            await workflow.execute_activity(
                payment_gateway,
                line,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=30),
                    maximum_attempts=3,
                    non_retryable_error_types=["INSUFFICIENT_FUNDS"],
                ),
            )
            return "SUCCESS"
        except ActivityError as e:
            workflow.logger.warning(f"Payment failed for line item {line}: {e}")
            return f"ERROR-{e.cause.message}" if e.cause else "ERROR"


@workflow.defn
class InvoiceWorkflow:
    def __init__(self) -> None:
        self.status: str = "INITIALIZING"
        self.invoice: dict = {}

    @workflow.signal
    async def ApproveInvoice(self) -> None:
        self.status = "APPROVED"

    @workflow.signal
    async def RejectInvoice(self) -> None:
        self.status = "REJECTED"

    @workflow.query
    def GetInvoiceStatus(self) -> str:
        return self.status

    @workflow.query
    def GetInvoiceData(self) -> dict:
        return self.invoice

    @workflow.run
    async def run(self, invoice: dict) -> str:
        self.invoice = invoice
        self.status = "PENDING-VALIDATION"
        workflow.logger.info(f"Starting workflow for invoice {invoice.get('invoice_id')}")
        await workflow.execute_activity(
            validate_against_erp,
            invoice,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=30),
                maximum_attempts=5,
            ),
        )

        self.status = "PENDING-APPROVAL"
        workflow.logger.info(f"Waiting for approval for invoice {invoice.get('invoice_id')}")
        # Wait for the approval signal
        await workflow.wait_condition(
            lambda: self.status != "PENDING-APPROVAL",
            timeout=timedelta(days=5),
        )

        if self.status == "REJECTED":
            workflow.logger.info("REJECTED")
            return "REJECTED"

        workflow.logger.info(f"Invoice {invoice.get('invoice_id')} approved, processing line items")

        # Start all child workflows in parallel
        handles = []
        for line in invoice.get("lines", []):
            handle = await workflow.start_child_workflow(PayLineItem.run, line)
            workflow.logger.info(f"Started child workflow for line item {line}")
            handles.append(handle)

        workflow.logger.info(f"Waiting for {len(handles)} child workflows to complete")
        self.status = "PAYING"

        # Collect results
        failed_count = 0
        for handle in handles:
            result = await handle
            if result.startswith("ERROR"):
                workflow.logger.warning(f"Line item payment failed: {result}")
                failed_count += 1
            else:
                workflow.logger.info(f"Line item paid successfully")

        if failed_count > 0:
            workflow.logger.warning(f"{failed_count} line items failed to pay")
            self.status = "FAILED"
        else:
            workflow.logger.info("All line items paid successfully")
            self.status = "PAID"
        return self.status
