# ABOUTME: Interactive CLI for submitting invoices and handling approvals.
# Connects to Temporal only — no MCP client. All MCP work happens in the worker.

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Optional

import click
from temporalio.client import Client as TemporalClient
from temporalio.service import RPCError

from async_mcp.client_worker.models import ElicitationDetails, TaskTrackerInput
from async_mcp.client_worker.workflows import CLIENT_TASK_QUEUE, TaskTrackerWorkflow

TASK_TRACKER_PREFIX = "task-tracker-"


async def _start_task(client: TemporalClient, invoice_json: dict) -> str:
    """Start a TaskTrackerWorkflow and return its workflow ID."""
    # Generate a unique tracker workflow ID. The MCP task ID will be different
    # (returned by the server) and stored inside the workflow.
    workflow_id = f"{TASK_TRACKER_PREFIX}{uuid.uuid4()}"
    await client.start_workflow(
        TaskTrackerWorkflow.run,
        TaskTrackerInput(invoice_json=invoice_json),
        id=workflow_id,
        task_queue=CLIENT_TASK_QUEUE,
    )
    return workflow_id


async def _list_tasks(client: TemporalClient) -> list[dict]:
    """Return a list of running TaskTrackerWorkflow summaries."""
    tasks = []
    async for execution in client.list_workflows(
        f'WorkflowType = "TaskTrackerWorkflow" AND ExecutionStatus = "Running"'
    ):
        tasks.append({"id": execution.id, "status": str(execution.status)})
    return tasks


async def _find_pending_elicitation(
    client: TemporalClient,
) -> Optional[tuple[str, ElicitationDetails]]:
    """Find the first running task that has a pending elicitation.

    Returns (workflow_id, ElicitationDetails) or None.
    """
    tasks = await _list_tasks(client)
    for task in tasks:
        wf_id = task["id"]
        try:
            handle = client.get_workflow_handle(wf_id)
            details = await handle.query(TaskTrackerWorkflow.get_elicitation_details)
            if details is not None:
                return wf_id, details
        except (RPCError, Exception):
            continue
    return None


async def _handle_pending_elicitation(client: TemporalClient) -> bool:
    """If any task is awaiting human input, prompt the user and send the decision.

    Returns True if an elicitation was handled, False otherwise.
    """
    pending = await _find_pending_elicitation(client)
    if not pending:
        return False

    wf_id, details = pending
    click.echo(f"\nApproval needed for task {wf_id}:")
    click.echo(f"  {details.message}")

    # Extract choices from schema if available
    schema = details.schema or {}
    choices = []
    for field_name, field_schema in schema.get("properties", {}).items():
        enums = field_schema.get("enum", [])
        if enums:
            choices = [str(v) for v in enums]
            break

    if choices:
        choices_str = " / ".join(choices)
        prompt_text = f"  Decision [{choices_str}]"
    else:
        prompt_text = "  Decision"

    while True:
        try:
            raw = await asyncio.to_thread(input, f"{prompt_text}: ")
            decision = raw.strip().lower()
        except (EOFError, KeyboardInterrupt):
            click.echo("\n  (Cancelled)")
            return True

        if not choices or decision in choices:
            break
        click.echo(f"  Invalid choice. Options: {choices_str}")

    handle = client.get_workflow_handle(wf_id)
    await handle.signal(TaskTrackerWorkflow.user_decision, decision)
    click.echo(f"  Decision '{decision}' sent.")
    return True


async def run_ui(temporal_address: str) -> None:
    """Run the interactive CLI."""
    client = await TemporalClient.connect(temporal_address)
    click.echo("Invoice Processing Client")
    click.echo("Commands: submit <file>, list, quit\n")

    while True:
        # Before each prompt, surface any pending approvals
        try:
            handled = await _handle_pending_elicitation(client)
            if handled:
                click.echo("")
        except Exception as exc:
            click.echo(f"  [Warning: elicitation check failed: {exc}]")

        try:
            line = await asyncio.to_thread(input, "> ")
        except (EOFError, KeyboardInterrupt):
            break

        parts = line.strip().split(None, 1)
        if not parts:
            continue
        cmd, *rest = parts
        arg = rest[0] if rest else ""

        if cmd in ("quit", "exit", "q"):
            break

        elif cmd == "submit":
            if not arg:
                click.echo("  Usage: submit <invoice_file.json>")
                continue
            try:
                with open(arg) as f:
                    invoice_json = json.load(f)
                wf_id = await _start_task(client, invoice_json)
                click.echo(f"  Started: {wf_id}")
            except FileNotFoundError:
                click.echo(f"  File not found: {arg}")
            except json.JSONDecodeError as e:
                click.echo(f"  Invalid JSON: {e}")
            except Exception as e:
                click.echo(f"  Error: {e}")

        elif cmd == "list":
            tasks = await _list_tasks(client)
            if not tasks:
                click.echo("  No running tasks.")
            else:
                for t in tasks:
                    click.echo(f"  {t['id']}  {t['status']}")

        else:
            click.echo(f"  Unknown command: {cmd!r}")

    click.echo("Goodbye!")


@click.command()
@click.option(
    "--temporal-address",
    envvar="TEMPORAL_ADDRESS",
    default="localhost:7233",
    help="Temporal server address",
)
def main(temporal_address: str) -> None:
    """Start the invoice processing UI."""
    try:
        asyncio.run(run_ui(temporal_address))
    except KeyboardInterrupt:
        click.echo("\nGoodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
