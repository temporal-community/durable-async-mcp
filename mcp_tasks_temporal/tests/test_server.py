# ABOUTME: End-to-end test of the tasks-extension server over the in-memory transport.
# Drives the full lifecycle (create -> poll -> input_required -> update -> completed) with a fake backend.

from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.server.lowlevel.server import Server
from mcp.shared.memory import create_client_server_memory_streams

from mcp_tasks_temporal.backend import TaskState
from mcp_tasks_temporal.server import register_tasks_extension
from mcp_tasks_temporal.wire import InputRequest, InputResponse, client_capability_meta

META = {"_meta": client_capability_meta()}


class FakeBackend:
    """In-memory task state machine: working -> input_required -> (after input) completed."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.received_inputs: dict[str, dict[str, InputResponse]] = {}

    async def start(self, tool_name: str, arguments: dict[str, Any]) -> TaskState:
        tid = f"task-{len(self.tasks) + 1}"
        self.tasks[tid] = {"polls": 0, "decided": False, "cancelled": False}
        return TaskState(tid, "working", "t0", "t0", ttl_ms=60000, poll_interval_ms=500)

    async def get_state(self, task_id: str) -> TaskState:
        st = self.tasks[task_id]
        if st["cancelled"]:
            return TaskState(task_id, "cancelled", "t0", "t1")
        st["polls"] += 1
        if st["decided"]:
            return TaskState(
                task_id,
                "completed",
                "t0",
                "t1",
                result={
                    "content": [{"type": "text", "text": "PAID"}],
                    "isError": False,
                    "resultType": "complete",
                },
            )
        if st["polls"] == 1:
            return TaskState(task_id, "working", "t0", "t1", poll_interval_ms=500)
        return TaskState(
            task_id,
            "input_required",
            "t0",
            "t1",
            input_requests={
                "approval": InputRequest(
                    method="elicitation/create",
                    params={
                        "mode": "form",
                        "message": "Approve?",
                        "requestedSchema": {"type": "object"},
                    },
                )
            },
        )

    async def submit_input(
        self, task_id: str, input_responses: dict[str, InputResponse]
    ) -> None:
        self.received_inputs[task_id] = input_responses
        self.tasks[task_id]["decided"] = True

    async def cancel(self, task_id: str) -> None:
        self.tasks[task_id]["cancelled"] = True


async def _raw(session: ClientSession, method: str, params: dict[str, Any]) -> Any:
    return await session._dispatcher.send_raw_request(method, params, {})


async def _run(coro_with_session):
    backend = FakeBackend()
    server = Server("test-tasks")
    register_tasks_extension(server, backend, task_tools={"process"})
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        cr, cw = client_streams
        sr, sw = server_streams
        init_opts = server.create_initialization_options()
        async with anyio.create_task_group() as tg:

            async def run_server() -> None:
                await server.run(sr, sw, init_opts, raise_exceptions=False)

            tg.start_soon(run_server)
            async with ClientSession(cr, cw) as session:
                await session.initialize()
                await coro_with_session(session, backend)
            tg.cancel_scope.cancel()


def test_full_task_lifecycle():
    async def scenario(session: ClientSession, backend: FakeBackend) -> None:
        created = await _raw(
            session, "tools/call", {"name": "process", "arguments": {"x": 1}, **META}
        )
        assert created["resultType"] == "task"
        tid = created["taskId"]
        assert created["status"] == "working"

        g1 = await _raw(session, "tasks/get", {"taskId": tid, **META})
        assert g1["status"] == "working"

        g2 = await _raw(session, "tasks/get", {"taskId": tid, **META})
        assert g2["status"] == "input_required"
        assert g2["inputRequests"]["approval"]["method"] == "elicitation/create"

        ack = await _raw(
            session,
            "tasks/update",
            {
                "taskId": tid,
                "inputResponses": {
                    "approval": {"action": "accept", "content": {"value": "approve"}}
                },
                **META,
            },
        )
        assert ack == {}
        assert backend.received_inputs[tid]["approval"].content == {"value": "approve"}

        g3 = await _raw(session, "tasks/get", {"taskId": tid, **META})
        assert g3["status"] == "completed"
        assert g3["result"]["content"][0]["text"] == "PAID"

    _run_sync(scenario)


def test_cancel_is_cooperative():
    async def scenario(session: ClientSession, backend: FakeBackend) -> None:
        created = await _raw(
            session, "tools/call", {"name": "process", "arguments": {}, **META}
        )
        tid = created["taskId"]
        ack = await _raw(session, "tasks/cancel", {"taskId": tid, **META})
        assert ack == {}
        g = await _raw(session, "tasks/get", {"taskId": tid, **META})
        assert g["status"] == "cancelled"

    _run_sync(scenario)


def test_task_tool_requires_declared_extension():
    async def scenario(session: ClientSession, backend: FakeBackend) -> None:
        # No _meta capability declaration -> server must refuse to create a task.
        try:
            await _raw(session, "tools/call", {"name": "process", "arguments": {}})
            assert False, "expected an error when the extension is not declared"
        except Exception as e:
            assert "extension" in str(e).lower()

    _run_sync(scenario)


def _run_sync(scenario) -> None:
    anyio.run(_run, scenario)
