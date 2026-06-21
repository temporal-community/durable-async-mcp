# ABOUTME: Unit tests for the MCP wire activities against a fake session.
# Verifies each activity issues the right tasks/* request (with capability _meta) and maps the reply.

import anyio

from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.client.activities import MCPActivities
from mcp_tasks_temporal.wire import CLIENT_CAPABILITIES_KEY


class FakeDispatcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def send_raw_request(self, method, params, opts):
        self.calls.append((method, params))
        return self.responses.get(method, {})


class FakeSession:
    def __init__(self, responses):
        self._dispatcher = FakeDispatcher(responses)


def _acts(responses):
    a = MCPActivities()
    a.bind(FakeSession(responses))
    return a


def test_start_task_calls_tools_call_with_capability_and_returns_task_id():
    a = _acts(
        {"tools/call": {"resultType": "task", "taskId": "t-9", "status": "working"}}
    )
    tid = anyio.run(a.start_task, models.StartTaskInput("process", {"x": 1}))
    assert tid == "t-9"
    method, params = a._session._dispatcher.calls[0]
    assert method == "tools/call"
    assert params["name"] == "process" and params["arguments"] == {"x": 1}
    assert CLIENT_CAPABILITIES_KEY in params["_meta"]


def test_poll_task_maps_input_required():
    a = _acts(
        {
            "tasks/get": {
                "status": "input_required",
                "pollIntervalMs": 250,
                "inputRequests": {
                    "approval": {"method": "elicitation/create", "params": {}}
                },
            }
        }
    )
    res = anyio.run(a.poll_task, "t-1")
    assert res.status == "input_required"
    assert res.poll_interval_ms == 250
    assert "approval" in res.input_requests
    assert res.result is None


def test_poll_task_maps_completed_result():
    a = _acts(
        {
            "tasks/get": {
                "status": "completed",
                "result": {"content": [{"type": "text", "text": "PAID"}]},
            }
        }
    )
    res = anyio.run(a.poll_task, "t-1")
    assert res.status == "completed"
    assert res.result["content"][0]["text"] == "PAID"


def test_submit_task_input_sends_input_responses():
    a = _acts({"tasks/update": {}})
    responses = {"approval": {"action": "accept", "content": {"value": "approve"}}}
    anyio.run(a.submit_task_input, models.SubmitInput("t-1", responses))
    method, params = a._session._dispatcher.calls[0]
    assert method == "tasks/update"
    assert params["taskId"] == "t-1"
    assert params["inputResponses"] == responses


def test_cancel_task_sends_cancel():
    a = _acts({"tasks/cancel": {}})
    anyio.run(a.cancel_task, "t-1")
    method, params = a._session._dispatcher.calls[0]
    assert method == "tasks/cancel" and params["taskId"] == "t-1"
