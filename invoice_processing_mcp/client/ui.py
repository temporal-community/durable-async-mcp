# ABOUTME: Interactive CLI for submitting invoices and answering approval elicitations.
# Connects to Temporal only; renders the generic inputRequests surfaced by TaskTrackerWorkflow.

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Optional

import click
from temporalio.client import Client as TemporalClient
from temporalio.service import RPCError

from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.client.workflows import TaskTrackerWorkflow

INVOICE_TOOL = "process_invoice"
TASK_TRACKER_PREFIX = "task-tracker-"


async def _start_task(client: TemporalClient, invoice_json: dict) -> str:
    """Start a TaskTrackerWorkflow for an invoice and return its workflow ID."""
    workflow_id = f"{TASK_TRACKER_PREFIX}{uuid.uuid4()}"
    await client.start_workflow(
        TaskTrackerWorkflow.run,
        models.TaskTrackerInput(
            tool_name=INVOICE_TOOL, arguments={"invoice": invoice_json}
        ),
        id=workflow_id,
        task_queue=models.TASK_QUEUE,
    )
    return workflow_id


async def _list_tasks(client: TemporalClient) -> list[dict]:
    tasks = []
    async for execution in client.list_workflows(
        'WorkflowType = "TaskTrackerWorkflow" AND ExecutionStatus = "Running"'
    ):
        tasks.append({"id": execution.id, "status": str(execution.status)})
    return tasks


async def _find_pending(client: TemporalClient) -> Optional[tuple[str, dict]]:
    """Find the first running task with outstanding inputRequests; return (wf_id, inputRequests)."""
    for task in await _list_tasks(client):
        wf_id = task["id"]
        try:
            handle = client.get_workflow_handle(wf_id)
            pending = await handle.query(TaskTrackerWorkflow.get_pending_input)
            if pending:
                return wf_id, pending
        except (RPCError, Exception):
            continue
    return None


async def _prompt_for(req_params: dict) -> dict:
    """Prompt for one elicitation's input; return its `content` dict (matching requestedSchema)."""
    click.echo(f"  {req_params.get('message', 'Input required')}")
    schema = req_params.get("requestedSchema", {})
    properties = schema.get("properties", {})

    field = None
    choices = None
    for name, field_schema in properties.items():
        if field_schema.get("enum"):
            field, choices = name, [str(v) for v in field_schema["enum"]]
            break
    if field is None:
        field = next(iter(properties), "value")

    label = f"  {field} [{' / '.join(choices)}]" if choices else f"  {field}"
    while True:
        raw = await asyncio.to_thread(input, f"{label}: ")
        value = raw.strip()
        if not choices or value in choices:
            return {field: value}
        click.echo(f"  Invalid choice. Options: {' / '.join(choices)}")


async def _handle_pending(client: TemporalClient) -> bool:
    """If any task awaits input, prompt for each request and signal the decision."""
    found = await _find_pending(client)
    if not found:
        return False

    wf_id, input_requests = found
    click.echo(f"\nInput needed for task {wf_id}:")
    responses: dict[str, dict] = {}
    for key, request in input_requests.items():
        try:
            content = await _prompt_for(request.get("params", {}))
        except (EOFError, KeyboardInterrupt):
            click.echo("\n  (Cancelled)")
            return True
        responses[key] = {"action": "accept", "content": content}

    handle = client.get_workflow_handle(wf_id)
    await handle.signal(TaskTrackerWorkflow.user_decision, responses)
    click.echo("  Decision sent.")
    return True


async def run_ui(temporal_address: str) -> None:
    client = await TemporalClient.connect(temporal_address)
    click.echo("Invoice Processing Client")
    click.echo("Commands: submit <file>, list, quit\n")

    while True:
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
            try:
                if await _handle_pending(client):
                    click.echo("")
            except Exception as exc:
                click.echo(f"  [Warning: input check failed: {exc}]")

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
