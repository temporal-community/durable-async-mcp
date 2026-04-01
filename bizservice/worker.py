import asyncio
import os
import click
from temporalio.client import Client
from temporalio.worker import Worker

from bizservice import activities
from bizservice.workflows import InvoiceWorkflow, PayLineItem


@click.command()
@click.option("--task-queue", default="invoice-task-queue", help="Task queue name")
@click.option("--fail-validate", is_flag=True, help="Force validation failure")
@click.option("--fail-payment", is_flag=True, help="Force payment failure")
def main(task_queue: str, fail_validate: bool, fail_payment: bool) -> None:
    os.environ["FAIL_VALIDATE"] = "true" if fail_validate else "false"
    os.environ["FAIL_PAYMENT"] = "true" if fail_payment else "false"
    asyncio.run(run_worker(task_queue))


async def run_worker(task_queue: str) -> None:
    client = await Client.connect(os.getenv("TEMPORAL_ADDRESS", "localhost:7233"))
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[InvoiceWorkflow, PayLineItem],
        activities=[activities.validate_against_erp, activities.payment_gateway],
    )
    await worker.run()


if __name__ == "__main__":
    main()
