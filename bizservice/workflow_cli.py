# ABOUTME: CLI tool for interacting with InvoiceWorkflow directly via Temporal.
# Supports starting workflows, sending approval/rejection signals, and querying workflow state.

import argparse
import asyncio
import json
import sys
import uuid

from temporalio.client import Client

from bizservice.workflows import InvoiceWorkflow, PayLineItem

TASK_QUEUE = "invoice-task-queue"
DEFAULT_TEMPORAL_ADDRESS = "localhost:7233"
SAMPLE_INVOICE_PATH = "samples/invoice_acme.json"


async def start(client: Client, args: argparse.Namespace) -> None:
    """Start a new InvoiceWorkflow."""
    if args.invoice:
        invoice = json.loads(args.invoice)
    else:
        with open(SAMPLE_INVOICE_PATH) as f:
            invoice = json.load(f)

    workflow_id = args.id or f"invoice-{uuid.uuid4()}"
    handle = await client.start_workflow(
        InvoiceWorkflow.run,
        invoice,
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    print(f"Started workflow: {handle.id}")


async def approve(client: Client, args: argparse.Namespace) -> None:
    """Send the ApproveInvoice signal."""
    handle = client.get_workflow_handle(args.workflow_id)
    await handle.signal(InvoiceWorkflow.ApproveInvoice)
    print(f"Sent approve signal to {args.workflow_id}")


async def reject(client: Client, args: argparse.Namespace) -> None:
    """Send the RejectInvoice signal."""
    handle = client.get_workflow_handle(args.workflow_id)
    await handle.signal(InvoiceWorkflow.RejectInvoice)
    print(f"Sent reject signal to {args.workflow_id}")


async def status(client: Client, args: argparse.Namespace) -> None:
    """Query GetInvoiceStatus."""
    handle = client.get_workflow_handle(args.workflow_id)
    result = await handle.query(InvoiceWorkflow.GetInvoiceStatus)
    print(f"Status: {result}")


async def data(client: Client, args: argparse.Namespace) -> None:
    """Query GetInvoiceData."""
    handle = client.get_workflow_handle(args.workflow_id)
    result = await handle.query(InvoiceWorkflow.GetInvoiceData)
    print(json.dumps(result, indent=2))


async def force_pay(client: Client, args: argparse.Namespace) -> None:
    """Send the ForcePayment signal to a PayLineItem child workflow."""
    handle = client.get_workflow_handle(args.workflow_id)
    await handle.signal(PayLineItem.ForcePayment)
    print(f"Sent force-pay signal to {args.workflow_id}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Interact with InvoiceWorkflow via Temporal")
    parser.add_argument(
        "--address", default=DEFAULT_TEMPORAL_ADDRESS, help="Temporal server address"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start
    sp_start = subparsers.add_parser("start", help="Start a new invoice workflow")
    sp_start.add_argument("--invoice", help="Invoice JSON string (default: samples/invoice_acme.json)")
    sp_start.add_argument("--id", help="Workflow ID (default: auto-generated)")

    # approve
    sp_approve = subparsers.add_parser("approve", help="Approve an invoice")
    sp_approve.add_argument("workflow_id", help="Workflow ID to approve")

    # reject
    sp_reject = subparsers.add_parser("reject", help="Reject an invoice")
    sp_reject.add_argument("workflow_id", help="Workflow ID to reject")

    # status
    sp_status = subparsers.add_parser("status", help="Query invoice status")
    sp_status.add_argument("workflow_id", help="Workflow ID to query")

    # data
    sp_data = subparsers.add_parser("data", help="Query invoice data")
    sp_data.add_argument("workflow_id", help="Workflow ID to query")

    # force-pay
    sp_force_pay = subparsers.add_parser("force-pay", help="Force immediate payment on a PayLineItem child workflow")
    sp_force_pay.add_argument("workflow_id", help="Child workflow ID to force payment on")

    args = parser.parse_args()
    client = await Client.connect(args.address)

    commands = {
        "start": start,
        "approve": approve,
        "reject": reject,
        "status": status,
        "data": data,
        "force-pay": force_pay,
    }
    await commands[args.command](client, args)


if __name__ == "__main__":
    asyncio.run(main())
