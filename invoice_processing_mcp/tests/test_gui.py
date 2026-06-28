# ABOUTME: Tests the GUI's Temporal-facing helpers and form->inputResponses logic with fakes.
# No NiceGUI rendering — the row mapping, response building, and signal dispatch are what we verify.

from datetime import datetime, timezone
from typing import Any

from invoice_processing_mcp.client.gui import (
    build_responses,
    fetch_po_rows,
    randomize_invoice,
    start_po,
    submit_decision,
)
from mcp_tasks_temporal.client import models


class FakeExecution:
    def __init__(self, wf_id: str, status: str, start_time: datetime) -> None:
        self.id = wf_id
        self.status = status
        self.start_time = start_time


class FakeHandle:
    def __init__(self, wf_id: str, queries: dict, signals: list) -> None:
        self.wf_id = wf_id
        self._queries = queries
        self._signals = signals

    async def query(self, method: Any) -> Any:
        name = getattr(method, "__name__", str(method))
        if name not in self._queries:
            raise RuntimeError(f"no fake query for {name}")
        return self._queries[name]

    async def signal(self, method: Any, *args: Any) -> None:
        name = getattr(method, "__name__", str(method))
        self._signals.append((self.wf_id, name, args[0] if args else None))


class FakeClient:
    def __init__(self, executions: list, handles: dict) -> None:
        self._executions = executions
        self._handles = handles
        self.started: list = []

    async def list_workflows(self, query: str):
        for ex in self._executions:
            yield ex

    def get_workflow_handle(self, wf_id: str) -> FakeHandle:
        return self._handles[wf_id]

    async def start_workflow(
        self, run: Any, arg: Any, *, id: str, task_queue: str
    ) -> None:
        self.started.append((id, arg, task_queue))


def _utc(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


async def test_fetch_po_rows_maps_task_status_newest_first():
    signals: list = []
    execs = [
        FakeExecution("po-1", "RUNNING", _utc(2)),
        FakeExecution("po-2", "COMPLETED", _utc(1)),
        FakeExecution("po-3", "RUNNING", _utc(3)),  # just started: no child yet
    ]
    handles = {
        "po-1": FakeHandle(
            "po-1",
            {"get_progress": {"po_id": "PO-aaa", "payment_workflow_id": "tt-1"}},
            signals,
        ),
        "tt-1": FakeHandle("tt-1", {"get_status": "input_required"}, signals),
        "po-2": FakeHandle(
            "po-2",
            {"get_progress": {"po_id": "PO-bbb", "payment_workflow_id": "tt-2"}},
            signals,
        ),
        "tt-2": FakeHandle("tt-2", {"get_status": "completed"}, signals),
        "po-3": FakeHandle(
            "po-3",
            {"get_progress": {"po_id": "PO-ccc", "payment_workflow_id": None}},
            signals,
        ),
    }
    rows = await fetch_po_rows(FakeClient(execs, handles))

    assert [r["po_wf_id"] for r in rows] == ["po-3", "po-1", "po-2"]  # newest first
    assert rows[0]["task_status"] == "starting"  # no child workflow yet
    assert rows[1]["task_status"] == "input_required"
    assert rows[1]["label"] == "po-1"  # first 10 chars of the workflow ID
    assert rows[1]["child_id"] == "tt-1"
    assert rows[2]["task_status"] == "completed"


def test_build_responses_accept_drops_blank_optional():
    reqs = {"cost-center-coding": {}}
    values = {"cost-center-coding": {"cost_center": "CC-1000", "memo": ""}}
    out = build_responses(reqs, values)
    assert out == {
        "cost-center-coding": {
            "action": "accept",
            "content": {"cost_center": "CC-1000"},
        }
    }


def test_build_responses_enum_value():
    out = build_responses({"approval": {}}, {"approval": {"value": "approve"}})
    assert out["approval"] == {"action": "accept", "content": {"value": "approve"}}


def test_build_responses_decline_has_no_content():
    out = build_responses(
        {"approval": {}}, {"approval": {"value": "approve"}}, action="decline"
    )
    assert out["approval"] == {"action": "decline", "content": None}


async def test_submit_decision_signals_child():
    signals: list = []
    handles = {"tt-1": FakeHandle("tt-1", {}, signals)}
    responses = {"approval": {"action": "accept", "content": {"value": "approve"}}}
    await submit_decision(FakeClient([], handles), "tt-1", responses)
    assert signals == [("tt-1", "user_decision", responses)]


def test_randomize_invoice_unique_id_and_perturbed_amounts():
    base = {
        "invoice_id": "INV-900",
        "customer": "Globex",
        "lines": [
            {"description": "a", "amount": 100.0, "due_date": "2020-01-01T00:00:00Z"},
            {"description": "b", "amount": 200.0, "due_date": "2020-01-01T00:00:00Z"},
        ],
    }
    out = randomize_invoice(base)

    num = int(out["invoice_id"].removeprefix("INV-"))
    assert 0 <= num <= 9999
    assert 90.0 <= out["lines"][0]["amount"] <= 110.0  # within ±10%
    assert 180.0 <= out["lines"][1]["amount"] <= 220.0
    # original is not mutated (deep copy)
    assert base["invoice_id"] == "INV-900"
    assert base["lines"][0]["amount"] == 100.0


async def test_start_po_starts_workflow():
    client = FakeClient([], {})
    invoice = {"invoice_id": "INV-1", "customer": "X", "lines": []}
    wf_id = await start_po(client, invoice)

    assert wf_id.startswith("po-")
    assert len(client.started) == 1
    sid, order, task_queue = client.started[0]
    assert sid == wf_id
    assert order["invoice"] == invoice
    assert order["po_id"].startswith("PO-")
    assert order["requester"]
    assert task_queue == models.TASK_QUEUE
