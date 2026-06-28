# ABOUTME: Deterministic tests for InvoiceTaskBackend (workflow-state->task-state mapping, signals) and server wiring.
# Uses a fake Temporal client/handle and FastMCP's in-memory transport — no Temporal server needed.

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import anyio
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

from invoice_processing_mcp.server.invoice_backend import InvoiceTaskBackend
from invoice_processing_mcp.server.server import build_server

INVOICE = {
    "invoice_id": "INV-1",
    "customer": "Acme",
    "lines": [
        {"description": "widget", "amount": 100.0, "due_date": "2020-01-01T00:00:00Z"}
    ],
}


class FakeHandle:
    def __init__(self, statuses: list[str], invoice: dict, final: str = "PAID") -> None:
        self.statuses = list(statuses)
        self.invoice = invoice
        self.final = final
        self.signals: list[str] = []
        self.signal_args: list[Any] = []
        self.cancelled = False
        self.start_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def describe(self) -> Any:
        return SimpleNamespace(start_time=self.start_time)

    async def query(self, q: Any) -> Any:
        name = getattr(q, "__name__", str(q))
        if "Status" in name:
            return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return self.invoice

    async def signal(self, s: Any, *args: Any) -> None:
        self.signals.append(getattr(s, "__name__", str(s)))
        if args:
            self.signal_args.append(args[0])

    async def result(self) -> str:
        return self.final

    async def cancel(self) -> None:
        self.cancelled = True


class FakeClient:
    def __init__(self, handle: FakeHandle) -> None:
        self.handle = handle
        self.started: list[tuple] = []

    async def start_workflow(
        self, run: Any, arg: Any, *, id: str, task_queue: str
    ) -> None:
        self.started.append((id, arg, task_queue))

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        return self.handle


def _backend(statuses, final="PAID"):
    return InvoiceTaskBackend(FakeClient(FakeHandle(statuses, INVOICE, final)))


# --- backend mapping unit tests ---


def test_start_starts_workflow_and_returns_working_state():
    client = FakeClient(FakeHandle(["PENDING-VALIDATION"], INVOICE))
    backend = InvoiceTaskBackend(client)
    snap = anyio.run(backend.start, "process_invoice", {"invoice": INVOICE})
    assert snap.status == "working"
    assert snap.task_id.startswith("invoice-")
    assert client.started[0][1] == INVOICE
    assert client.started[0][2] == "invoice-task-queue"


def test_get_state_pending_approval_builds_elicitation():
    state = anyio.run(_backend(["PENDING-APPROVAL"]).get_state, "invoice-x")
    assert state.status == "input_required"
    req = state.input_requests["approval"]
    assert req.method == "elicitation/create"
    assert req.params["requestedSchema"]["properties"]["value"]["enum"] == [
        "approve",
        "reject",
    ]
    assert state.result is None


def test_get_state_pending_cost_center_builds_distinct_request():
    state = anyio.run(_backend(["PENDING-COST-CENTER"]).get_state, "invoice-x")
    assert state.status == "input_required"
    assert "approval" not in state.input_requests
    req = state.input_requests["cost-center-coding"]
    assert req.method == "elicitation/create"
    props = req.params["requestedSchema"]["properties"]
    assert "cost_center" in props and "memo" in props
    assert req.params["requestedSchema"]["required"] == ["cost_center"]


def test_get_state_paid_builds_result():
    state = anyio.run(_backend(["PAID"]).get_state, "invoice-x")
    assert state.status == "completed"
    assert "PAID" in state.result["content"][0]["text"]


def test_get_state_failed_builds_error():
    state = anyio.run(_backend(["FAILED"]).get_state, "invoice-x")
    assert state.status == "failed"
    assert state.error["message"]


def test_submit_input_approve_signals_approve():
    handle = FakeHandle(["PENDING-APPROVAL"], INVOICE)
    backend = InvoiceTaskBackend(FakeClient(handle))
    anyio.run(
        backend.submit_input,
        "invoice-x",
        {"approval": {"action": "accept", "content": {"value": "approve"}}},
    )
    assert "ApproveInvoice" in handle.signals


def test_submit_input_reject_signals_reject():
    handle = FakeHandle(["PENDING-APPROVAL"], INVOICE)
    backend = InvoiceTaskBackend(FakeClient(handle))
    anyio.run(
        backend.submit_input,
        "invoice-x",
        {"approval": {"action": "accept", "content": {"value": "reject"}}},
    )
    assert "RejectInvoice" in handle.signals


def test_submit_input_cost_center_signals_submit_with_content():
    handle = FakeHandle(["PENDING-COST-CENTER"], INVOICE)
    backend = InvoiceTaskBackend(FakeClient(handle))
    anyio.run(
        backend.submit_input,
        "invoice-x",
        {
            "cost-center-coding": {
                "action": "accept",
                "content": {"cost_center": "CC-1000", "memo": "Q3"},
            }
        },
    )
    assert "SubmitCostCenter" in handle.signals
    assert handle.signal_args[0] == {"cost_center": "CC-1000", "memo": "Q3"}


def test_submit_input_cost_center_decline_signals_reject():
    handle = FakeHandle(["PENDING-COST-CENTER"], INVOICE)
    backend = InvoiceTaskBackend(FakeClient(handle))
    anyio.run(
        backend.submit_input,
        "invoice-x",
        {"cost-center-coding": {"action": "decline", "content": None}},
    )
    assert "RejectInvoice" in handle.signals


def test_wait_result_returns_terminal_call_tool_result():
    result = anyio.run(_backend(["PAYING"], final="PAID").wait_result, "invoice-x")
    assert result["content"][0]["text"] == "Invoice processing result: PAID"
    assert result["isError"] is False


def test_cancel_cancels_workflow():
    handle = FakeHandle(["PAYING"], INVOICE)
    anyio.run(InvoiceTaskBackend(FakeClient(handle)).cancel, "invoice-x")
    assert handle.cancelled


# --- server wiring smoke test over FastMCP's in-memory transport (single gate) ---


def test_server_wiring_single_gate_flow():
    handle = FakeHandle(["PENDING-APPROVAL"], INVOICE, final="PAID")
    client = FakeClient(handle)

    async def handler(message, response_type, params, context):
        schema = getattr(params, "requestedSchema", None) or {}
        assert schema["x-request-key"] == "approval"
        return ElicitResult(action="accept", content={"value": "approve"})

    async def scenario() -> None:
        mcp = build_server(client)
        async with Client(mcp, elicitation_handler=handler) as c:
            tools = await c.list_tools()
            assert any(t.name == "process_invoice" for t in tools)

            task = await c.call_tool("process_invoice", {"invoice": INVOICE}, task=True)
            assert task.task_id.startswith("invoice-")
            status = await c.get_task_status(task.task_id)
            assert status.status == "input_required"

            result = await c.get_task_result(task.task_id)
            assert "ApproveInvoice" in handle.signals
            assert "PAID" in result["content"][0]["text"]

    anyio.run(scenario)
