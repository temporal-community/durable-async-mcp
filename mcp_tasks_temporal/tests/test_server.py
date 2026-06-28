# ABOUTME: End-to-end test of the tasks server over FastMCP's in-memory transport with a fake backend.
# Drives create -> poll -> input_required(server-push elicitation) -> result, including a two-gate flow.

from __future__ import annotations

from typing import Any

import anyio
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult
from fastmcp.server.tasks import TaskConfig

from mcp_tasks_temporal.backend import InputRequest, TaskState
from mcp_tasks_temporal.server import register_tasks_extension

NOW = "2026-06-28T00:00:00+00:00"


def _state(task_id: str, status: str, **kw: Any) -> TaskState:
    return TaskState(
        task_id, status, NOW, NOW, ttl_ms=60000, poll_interval_ms=500, **kw
    )


def _input_request(key: str, message: str, schema: dict[str, Any]) -> InputRequest:
    return InputRequest(
        method="elicitation/create",
        params={"mode": "form", "message": message, "requestedSchema": schema},
    )


class FakeBackend:
    """A scripted backend: walks an explicit list of gates, then completes.

    `gates` is an ordered list of (key, message, schema); each is elicited once via tasks/result.
    """

    def __init__(self, gates: list[tuple[str, str, dict[str, Any]]]) -> None:
        self._gates = gates
        self.started: list[tuple[str, dict]] = []
        self.received: list[tuple[str, dict]] = []
        self.cancelled = False
        self._answered = 0

    async def start(self, tool_name: str, arguments: dict[str, Any]) -> TaskState:
        self.started.append((tool_name, arguments))
        return _state("task-1", "working", status_message="started")

    async def get_state(self, task_id: str) -> TaskState:
        if self.cancelled:
            return _state(task_id, "cancelled")
        if self._answered >= len(self._gates):
            return _state(
                task_id,
                "completed",
                result={
                    "content": [{"type": "text", "text": "PAID"}],
                    "isError": False,
                },
            )
        key, message, schema = self._gates[self._answered]
        return _state(
            task_id,
            "input_required",
            input_requests={key: _input_request(key, message, schema)},
        )

    async def submit_input(self, task_id: str, input_responses: dict[str, Any]) -> None:
        self.received.append((task_id, input_responses))
        self._answered += 1

    async def wait_result(self, task_id: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "PAID"}], "isError": False}

    async def cancel(self, task_id: str) -> None:
        self.cancelled = True


def _build(backend: FakeBackend) -> FastMCP:
    mcp = FastMCP("test-tasks")

    @mcp.tool(task=TaskConfig(mode="required"))
    async def process(x: int) -> dict:
        return {"x": x}

    register_tasks_extension(mcp, backend, task_tools={"process"})
    return mcp


_APPROVAL_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string", "enum": ["approve", "reject"]}},
    "required": ["value"],
}
_COST_CENTER_SCHEMA = {
    "type": "object",
    "properties": {"cost_center": {"type": "string"}, "memo": {"type": "string"}},
    "required": ["cost_center"],
}


def test_single_gate_flow():
    backend = FakeBackend([("approval", "Approve?", _APPROVAL_SCHEMA)])
    seen_schema: dict[str, Any] = {}

    async def handler(message, response_type, params, context):
        seen_schema.update(getattr(params, "requestedSchema", None) or {})
        return ElicitResult(action="accept", content={"value": "approve"})

    async def scenario() -> None:
        mcp = _build(backend)
        async with Client(mcp, elicitation_handler=handler) as c:
            task = await c.call_tool("process", {"x": 1}, task=True)
            assert task.task_id == "task-1"
            status = await c.get_task_status(task.task_id)
            assert status.status == "input_required"
            result = await c.get_task_result(task.task_id)

        # The server injected routing hints into the pushed elicitation schema.
        assert seen_schema["x-task-id"] == "task-1"
        assert seen_schema["x-request-key"] == "approval"
        # The answer reached the backend under the right key.
        assert backend.received[0][1]["approval"]["content"] == {"value": "approve"}
        assert result["content"][0]["text"] == "PAID"

    anyio.run(scenario)


def test_two_gate_flow():
    """A large invoice traverses approval, then a distinct cost-center gate, then completes."""
    backend = FakeBackend(
        [
            ("approval", "Approve?", _APPROVAL_SCHEMA),
            ("cost-center-coding", "Assign cost center", _COST_CENTER_SCHEMA),
        ]
    )
    seen_keys: list[str] = []

    async def handler(message, response_type, params, context):
        schema = getattr(params, "requestedSchema", None) or {}
        key = schema["x-request-key"]
        seen_keys.append(key)
        if key == "approval":
            return ElicitResult(action="accept", content={"value": "approve"})
        return ElicitResult(
            action="accept", content={"cost_center": "CC-1000", "memo": "Q3"}
        )

    async def scenario() -> None:
        mcp = _build(backend)
        async with Client(mcp, elicitation_handler=handler) as c:
            task = await c.call_tool("process", {"x": 1}, task=True)
            # Gate 1: approval (client cancels tasks/result after the elicitation resolves).
            await c.get_task_result(task.task_id)
            # Gate 2: cost-center coding, then terminal.
            result = await c.get_task_result(task.task_id)

        assert seen_keys == ["approval", "cost-center-coding"]
        assert backend.received[0][1]["approval"]["content"]["value"] == "approve"
        assert backend.received[1][1]["cost-center-coding"]["content"] == {
            "cost_center": "CC-1000",
            "memo": "Q3",
        }
        assert result["content"][0]["text"] == "PAID"

    anyio.run(scenario)


def test_cancel_is_cooperative():
    backend = FakeBackend([("approval", "Approve?", _APPROVAL_SCHEMA)])

    async def scenario() -> None:
        mcp = _build(backend)
        async with Client(mcp) as c:
            task = await c.call_tool("process", {}, task=True)
            await c.cancel_task(task.task_id)
            assert backend.cancelled
            status = await c.get_task_status(task.task_id)
            assert status.status == "cancelled"

    anyio.run(scenario)
