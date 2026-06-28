# ABOUTME: NiceGUI status board for purchase orders and their invoice-task lifecycle state.
# Temporal-client only: lists PurchaseOrderWorkflows, shows each invoice task's status, answers HITL.

from __future__ import annotations

import copy
import json
import random
import uuid
from typing import Any

import click
from nicegui import app, ui
from temporalio.client import Client as TemporalClient

from invoice_processing_mcp.client.purchase_order_workflow import PurchaseOrderWorkflow
from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.client.workflows import TaskTrackerWorkflow

PO_WORKFLOW_TYPE = "PurchaseOrderWorkflow"
SIMPLE_SAMPLE = "samples/invoice1.json"
LARGE_SAMPLE = "samples/invoice_large.json"
DEFAULT_REQUESTER = "buyer@acme.test"


def randomize_invoice(invoice: dict[str, Any]) -> dict[str, Any]:
    """A copy with a random INV-#### id and each line amount perturbed by up to ±10%.

    The ±10% keeps the large sample comfortably over the cost-center threshold and the simple
    sample under it, so which HITL gates fire is unchanged.
    """
    inv = copy.deepcopy(invoice)
    inv["invoice_id"] = f"INV-{random.randint(0, 9999):04d}"
    for line in inv.get("lines", []):
        line["amount"] = round(line.get("amount", 0) * random.uniform(0.9, 1.1), 2)
    return inv


async def start_po(client: TemporalClient, invoice: dict[str, Any]) -> str:
    """Start a PurchaseOrderWorkflow for an invoice; return its workflow id."""
    workflow_id = f"po-{uuid.uuid4()}"
    order = {
        "po_id": f"PO-{uuid.uuid4().hex[:8]}",
        "requester": DEFAULT_REQUESTER,
        "invoice": invoice,
    }
    await client.start_workflow(
        PurchaseOrderWorkflow.run,
        order,
        id=workflow_id,
        task_queue=models.TASK_QUEUE,
    )
    return workflow_id


# --- Temporal-facing helpers (pure of NiceGUI; unit-tested with a fake client) ---


def _start_time_key(execution: Any) -> float:
    start = getattr(execution, "start_time", None)
    return start.timestamp() if start is not None else 0.0


async def fetch_po_rows(client: TemporalClient) -> list[dict[str, Any]]:
    """One row per PurchaseOrderWorkflow (running or closed), newest first, carrying the
    invoice task's lifecycle state (queried from the child TaskTrackerWorkflow)."""
    executions = [
        ex async for ex in client.list_workflows(f'WorkflowType = "{PO_WORKFLOW_TYPE}"')
    ]
    executions.sort(key=_start_time_key, reverse=True)

    rows: list[dict[str, Any]] = []
    for ex in executions:
        po_wf_id = ex.id
        try:
            progress = await client.get_workflow_handle(po_wf_id).query(
                PurchaseOrderWorkflow.get_progress
            )
        except Exception:
            progress = {}
        child_id = progress.get("payment_workflow_id")
        task_status = "starting"
        if child_id:
            try:
                task_status = await client.get_workflow_handle(child_id).query(
                    TaskTrackerWorkflow.get_status
                )
            except Exception:
                task_status = "unknown"
        status = getattr(ex, "status", None)
        rows.append(
            {
                "po_wf_id": po_wf_id,
                "label": po_wf_id[:10],  # first 10 chars of the workflow ID
                # WorkflowExecutionStatus is an IntEnum — use .name (RUNNING/COMPLETED/…),
                # not str() (which is the integer code on Python 3.11+).
                "po_status": getattr(status, "name", None) or str(status or ""),
                "child_id": child_id,
                "task_status": task_status,
            }
        )
    return rows


async def get_pending(client: TemporalClient, child_id: str) -> dict[str, Any] | None:
    """The outstanding inputRequests for a task, or None."""
    try:
        return await client.get_workflow_handle(child_id).query(
            TaskTrackerWorkflow.get_pending_input
        )
    except Exception:
        return None


def build_responses(
    input_requests: dict[str, Any],
    values: dict[str, dict[str, Any]],
    action: str = "accept",
) -> dict[str, Any]:
    """Build the inputResponses map for tasks/update.

    `values` is {request_key: {field_name: value}}; blank/None fields are dropped. A non-accept
    action (decline/cancel) carries no content.
    """
    responses: dict[str, Any] = {}
    for key in input_requests:
        if action != "accept":
            responses[key] = {"action": action, "content": None}
            continue
        content = {
            field: value
            for field, value in (values.get(key) or {}).items()
            if value not in (None, "")
        }
        responses[key] = {"action": "accept", "content": content}
    return responses


async def submit_decision(
    client: TemporalClient, child_id: str, responses: dict[str, Any]
) -> None:
    """Signal the task's child workflow with the answer to its pending inputRequests."""
    await client.get_workflow_handle(child_id).signal(
        TaskTrackerWorkflow.user_decision, responses
    )


# --- NiceGUI app ---

_client: TemporalClient | None = None
_rows: list[dict[str, Any]] = []
_selected_child_id: str | None = None
_pending: dict[str, Any] | None = None
_pending_sig: tuple[str, ...] | None = None
_refreshing = False
_temporal_address = "localhost:7233"
_refresh_seconds = 2.0


def _sig(pending: dict[str, Any] | None) -> tuple[str, ...] | None:
    """A signature of the pending question's keys — changes only when the question changes,
    so the timer can leave an open answer form untouched while the same question is pending.
    """
    return tuple(sorted(pending)) if pending else None


_STATUS_COLORS = {
    "working": "text-amber-600",
    "input_required": "text-blue-600",
    "completed": "text-green-600",
    "failed": "text-red-600",
    "cancelled": "text-gray-500",
    "starting": "text-gray-500",
}


async def _select(child_id: str) -> None:
    global _selected_child_id, _pending, _pending_sig
    _selected_child_id = child_id
    _pending = await get_pending(_client, child_id) if _client is not None else None
    _pending_sig = _sig(_pending)
    question_pane.refresh()


def _close_pane() -> None:
    """Dismiss the question pane without answering — the task stays input_required."""
    global _selected_child_id, _pending, _pending_sig
    _selected_child_id = None
    _pending = None
    _pending_sig = None
    question_pane.refresh()


async def _submit_sample(path: str) -> None:
    if _client is None:
        ui.notify("Not connected to Temporal yet.", type="warning")
        return
    try:
        with open(path) as f:
            invoice = randomize_invoice(json.load(f))
        await start_po(_client, invoice)
    except Exception as exc:
        ui.notify(f"Submit failed: {exc}", type="negative")
        return
    ui.notify(f"Submitted {invoice['invoice_id']}")
    await _refresh_all()


@ui.refreshable
def question_pane() -> None:
    if not _selected_child_id:
        ui.label("Select an 'input_required' task to answer its question.").classes(
            "text-gray-500 italic"
        )
        return
    if not _pending:
        ui.label("This task is no longer awaiting input.").classes("text-gray-500")
        return

    field_values: dict[str, dict[str, Any]] = {}
    for key, request in _pending.items():
        params = request.get("params", {})
        schema = params.get("requestedSchema", {})
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))

        ui.label(params.get("message", "Input required")).classes("font-medium")
        field_values[key] = {}
        for name, field_schema in properties.items():
            label = name + ("" if name in required else " (optional)")
            el: Any
            if field_schema.get("enum"):
                el = ui.select(
                    [str(v) for v in field_schema["enum"]], label=label
                ).classes("w-64")
            else:
                el = ui.input(label=label).classes("w-64")
            field_values[key][name] = el

    child_id = _selected_child_id

    async def _answer(action: str) -> None:
        global _selected_child_id, _pending, _pending_sig
        values = {
            key: {name: el.value for name, el in fields.items()}
            for key, fields in field_values.items()
        }
        responses = build_responses(_pending or {}, values, action=action)
        if _client is not None and child_id is not None:
            await submit_decision(_client, child_id, responses)
        _selected_child_id = None
        _pending = None
        _pending_sig = None
        question_pane.refresh()
        ui.notify(f"Decision sent ({action}).")

    with ui.row():
        ui.button("Submit", on_click=lambda: _answer("accept"))
        ui.button("Close", on_click=_close_pane).props("flat color=grey")


@ui.refreshable
def po_list() -> None:
    with ui.row().classes("items-center gap-4 border-b pb-1 font-semibold"):
        ui.label("Purchase order").classes("w-48")
        ui.label("PO workflow").classes("w-28")
        ui.label("Invoice task").classes("w-28")
    if not _rows:
        ui.label("No purchase orders yet.").classes("text-gray-500 italic")
        return
    for row in _rows:
        with ui.row().classes("items-center gap-4 border-b py-1"):
            ui.label(row["label"]).classes("font-mono w-48")
            ui.label(row["po_status"]).classes("text-gray-500 w-28")
            status = row["task_status"]
            color = _STATUS_COLORS.get(status, "")
            if status == "input_required" and row["child_id"]:
                cid = row["child_id"]
                ui.label(status).classes(
                    f"{color} underline cursor-pointer font-medium"
                ).on("click", lambda cid=cid: _select(cid))
            else:
                ui.label(status).classes(color)


async def _ensure_client() -> None:
    """Connect lazily so the page still boots if Temporal isn't reachable yet (it retries)."""
    global _client
    if _client is None:
        try:
            _client = await TemporalClient.connect(_temporal_address)
        except Exception:
            _client = None


async def _refresh_all() -> None:
    global _rows, _pending, _pending_sig, _refreshing
    if _refreshing:
        return
    await _ensure_client()
    client = _client
    if client is None:
        return
    _refreshing = True
    try:
        _rows = await fetch_po_rows(client)
        po_list.refresh()
        # Re-render the question pane ONLY when the pending question actually changes —
        # otherwise the 2s tick would rebuild (and close) an open answer form mid-selection.
        if _selected_child_id:
            new_pending = await get_pending(client, _selected_child_id)
            if _sig(new_pending) != _pending_sig:
                _pending = new_pending
                _pending_sig = _sig(new_pending)
                question_pane.refresh()
    finally:
        _refreshing = False


@ui.page("/")
def index() -> None:
    # Explicit route (not the auto-index) so NiceGUI never re-runs this script to render the page.
    ui.label("Purchase Orders — invoice task lifecycle").classes("text-xl font-bold")
    with ui.card().classes("w-full"):
        question_pane()
    with ui.card().classes("w-full"):
        po_list()
    with ui.card().classes("w-full"):
        ui.label("Submit a new purchase order").classes("font-medium")
        with ui.row():
            ui.button("Submit simple", on_click=lambda: _submit_sample(SIMPLE_SAMPLE))
            ui.button("Submit large", on_click=lambda: _submit_sample(LARGE_SAMPLE))
    ui.timer(_refresh_seconds, _refresh_all)


app.on_startup(_refresh_all)


@click.command()
@click.option(
    "--temporal-address",
    envvar="TEMPORAL_ADDRESS",
    default="localhost:7233",
    help="Temporal server address",
)
@click.option("--port", default=8080, help="Port for the web UI")
@click.option(
    "--refresh-seconds",
    default=2.0,
    help="Refresh interval (≈ the task poll frequency)",
)
def main(temporal_address: str, port: int, refresh_seconds: float) -> None:
    """Start the purchase-order status board."""
    global _temporal_address, _refresh_seconds
    _temporal_address = temporal_address
    _refresh_seconds = refresh_seconds
    ui.run(port=port, reload=False, title="Invoice Tasks", show=False)


if __name__ in {"__main__", "__mp_main__"}:
    main()
