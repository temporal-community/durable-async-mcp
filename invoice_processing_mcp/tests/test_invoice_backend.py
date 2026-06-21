# ABOUTME: Deterministic tests for InvoiceTaskBackend (workflow-state->task-state mapping, signals) and server wiring.
# Uses a fake Temporal client/handle and the in-memory MCP transport — no Temporal server needed.

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from invoice_processing_mcp.server.invoice_backend import InvoiceTaskBackend
from invoice_processing_mcp.server.server import build_server
from mcp_tasks_temporal.wire import InputResponse, client_capability_meta

INVOICE = {
    "invoice_id": "INV-1",
    "customer": "Acme",
    "lines": [
        {"description": "widget", "amount": 100.0, "due_date": "2020-01-01T00:00:00Z"}
    ],
}
META = {"_meta": client_capability_meta()}


class FakeHandle:
    def __init__(self, statuses: list[str], invoice: dict) -> None:
        self.statuses = list(statuses)
        self.invoice = invoice
        self.signals: list[str] = []
        self.cancelled = False
        self.start_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def describe(self) -> Any:
        return SimpleNamespace(start_time=self.start_time)

    async def query(self, q: Any) -> Any:
        name = getattr(q, "__name__", str(q))
        if "Status" in name:
            return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return self.invoice

    async def signal(self, s: Any) -> None:
        self.signals.append(getattr(s, "__name__", str(s)))

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


def _backend(statuses):
    return InvoiceTaskBackend(FakeClient(FakeHandle(statuses, INVOICE)))


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
        {"approval": InputResponse(action="accept", content={"value": "approve"})},
    )
    assert "ApproveInvoice" in handle.signals


def test_submit_input_reject_signals_reject():
    handle = FakeHandle(["PENDING-APPROVAL"], INVOICE)
    backend = InvoiceTaskBackend(FakeClient(handle))
    anyio.run(
        backend.submit_input,
        "invoice-x",
        {"approval": InputResponse(action="accept", content={"value": "reject"})},
    )
    assert "RejectInvoice" in handle.signals


def test_cancel_cancels_workflow():
    handle = FakeHandle(["PAYING"], INVOICE)
    anyio.run(InvoiceTaskBackend(FakeClient(handle)).cancel, "invoice-x")
    assert handle.cancelled


# --- server wiring test over the in-memory transport ---


async def _raw(session, method, params):
    return await session._dispatcher.send_raw_request(method, params, {})


async def _server_scenario():
    handle = FakeHandle(["PENDING-APPROVAL", "PAID"], INVOICE)
    client = FakeClient(handle)
    server = build_server(client)
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        cr, cw = client_streams
        sr, sw = server_streams
        init = server.create_initialization_options()
        async with anyio.create_task_group() as tg:

            async def run_server():
                await server.run(sr, sw, init, raise_exceptions=False)

            tg.start_soon(run_server)
            async with ClientSession(cr, cw) as session:
                await session.initialize()

                tools = await _raw(session, "tools/list", {})
                assert any(t["name"] == "process_invoice" for t in tools["tools"])

                call_args = {
                    "name": "process_invoice",
                    "arguments": {"invoice": INVOICE},
                    **META,
                }
                created = await _raw(session, "tools/call", call_args)
                assert created["resultType"] == "task"
                tid = created["taskId"]

                g1 = await _raw(session, "tasks/get", {"taskId": tid, **META})
                assert g1["status"] == "input_required"
                assert "approval" in g1["inputRequests"]

                await _raw(
                    session,
                    "tasks/update",
                    {
                        "taskId": tid,
                        "inputResponses": {
                            "approval": {
                                "action": "accept",
                                "content": {"value": "approve"},
                            }
                        },
                        **META,
                    },
                )
                assert "ApproveInvoice" in handle.signals

                g2 = await _raw(session, "tasks/get", {"taskId": tid, **META})
                assert g2["status"] == "completed"
                assert "PAID" in g2["result"]["content"][0]["text"]
            tg.cancel_scope.cancel()


def test_server_wiring_full_flow():
    anyio.run(_server_scenario)
