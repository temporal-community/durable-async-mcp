# ABOUTME: Unit tests for the FastMCP-backed MCP wire activities against fake clients.
# Covers start/poll/get_result and the elicitation handler's x-request-key routing + reader-release.

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.client.elicitation import ElicitResult
from temporalio.testing import ActivityEnvironment

from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.client.activities import MCPActivities

SAMPLE_TASK_ID = "invoice-abc123"
SAMPLE_WF_ID = "task-tracker-wf-001"


def _acts(mcp_mock: Any = None, temporal_mock: Any = None) -> MCPActivities:
    a = MCPActivities(temporal_client=temporal_mock or MagicMock())
    a._mcp = mcp_mock or AsyncMock()
    return a


class _FakeParams:
    def __init__(self, schema: dict) -> None:
        self.requestedSchema = schema


class TestStartTask:
    async def test_calls_tool_with_task_and_returns_id(self):
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(return_value=MagicMock(task_id="t-9"))
        acts = _acts(mcp_mock=mcp)
        result = await ActivityEnvironment().run(
            acts.start_task,
            models.StartTaskInput("process_invoice", {"invoice": {"id": 1}}),
        )
        assert result == "t-9"
        mcp.call_tool.assert_awaited_once_with(
            "process_invoice", {"invoice": {"id": 1}}, task=True
        )


class TestPollTask:
    @pytest.mark.parametrize(
        "state", ["working", "input_required", "completed", "failed", "cancelled"]
    )
    async def test_returns_status_string(self, state: str):
        mcp = AsyncMock()
        mcp.get_task_status = AsyncMock(return_value=MagicMock(status=state))
        acts = _acts(mcp_mock=mcp)
        result = await ActivityEnvironment().run(acts.poll_task, SAMPLE_TASK_ID)
        assert result == state
        mcp.get_task_status.assert_awaited_once_with(SAMPLE_TASK_ID)


class TestGetTaskResult:
    async def test_returns_result_dict(self):
        raw = {"content": [{"type": "text", "text": "PAID"}], "isError": False}
        mcp = AsyncMock()
        mcp.get_task_result = AsyncMock(return_value=raw)
        acts = _acts(mcp_mock=mcp)
        result = await ActivityEnvironment().run(acts.get_task_result, SAMPLE_TASK_ID)
        assert result["content"][0]["text"] == "PAID"
        assert result["isError"] is False


class TestElicitationHandler:
    def _acts_with_active(self, decision: Any) -> MCPActivities:
        handle = AsyncMock()
        handle.query = AsyncMock(return_value=decision)
        temporal = MagicMock()
        temporal.get_workflow_handle = MagicMock(return_value=handle)
        acts = _acts(temporal_mock=temporal)
        acts._active_elicitations[SAMPLE_TASK_ID] = SAMPLE_WF_ID
        return acts

    async def test_signals_keyed_input_requests_and_returns_decision(self):
        acts = self._acts_with_active(
            {"approval": {"action": "accept", "content": {"value": "approve"}}}
        )
        handle = acts._temporal.get_workflow_handle.return_value
        schema = {
            "type": "object",
            "properties": {"value": {"type": "string", "enum": ["approve", "reject"]}},
            "x-task-id": SAMPLE_TASK_ID,
            "x-request-key": "approval",
        }
        result = await acts._elicitation_handler(
            "Approve INV-001?", None, _FakeParams(schema), None
        )

        method, payload = handle.signal.call_args.args
        assert method == "elicitation_received"
        assert "approval" in payload
        params = payload["approval"]["params"]
        assert params["message"] == "Approve INV-001?"
        assert "x-task-id" not in params["requestedSchema"]
        assert "x-request-key" not in params["requestedSchema"]

        assert isinstance(result, ElicitResult)
        assert result.action == "accept"
        assert result.content == {"value": "approve"}

    async def test_routes_cost_center_key(self):
        acts = self._acts_with_active(
            {
                "approval": {"action": "accept", "content": {"value": "approve"}},
                "cost-center-coding": {
                    "action": "accept",
                    "content": {"cost_center": "CC-1000"},
                },
            }
        )
        schema = {"x-task-id": SAMPLE_TASK_ID, "x-request-key": "cost-center-coding"}
        result = await acts._elicitation_handler(
            "Cost center?", None, _FakeParams(schema), None
        )
        # Only the active key's entry is consumed.
        assert result.content == {"cost_center": "CC-1000"}

    async def test_sets_event_when_decided(self):
        acts = self._acts_with_active(
            {"approval": {"action": "accept", "content": {"value": "approve"}}}
        )
        event = asyncio.Event()
        acts._elicitation_events[SAMPLE_TASK_ID] = event
        schema = {"x-task-id": SAMPLE_TASK_ID, "x-request-key": "approval"}
        await acts._elicitation_handler("Approve?", None, _FakeParams(schema), None)
        assert event.is_set()

    async def test_raises_when_no_decision(self):
        acts = self._acts_with_active(None)
        handle = acts._temporal.get_workflow_handle.return_value
        schema = {"x-task-id": SAMPLE_TASK_ID, "x-request-key": "approval"}
        with pytest.raises(RuntimeError, match="No decision yet"):
            await acts._elicitation_handler("Approve?", None, _FakeParams(schema), None)
        assert (
            handle.query.call_count == 1
        )  # checks exactly once, then releases the reader

    async def test_raises_when_missing_routing_hints(self):
        acts = _acts()
        with pytest.raises(RuntimeError, match="x-task-id"):
            await acts._elicitation_handler("Approve?", None, _FakeParams({}), None)

    async def test_raises_when_task_not_registered(self):
        acts = _acts()
        schema = {"x-task-id": "unknown", "x-request-key": "approval"}
        with pytest.raises(RuntimeError, match="No active elicitation"):
            await acts._elicitation_handler("Approve?", None, _FakeParams(schema), None)


class TestHandleElicitation:
    async def test_returns_handled_and_cancels_when_decided(self):
        handle = AsyncMock()
        handle.query = AsyncMock(
            return_value={
                "approval": {"action": "accept", "content": {"value": "approve"}}
            }
        )
        temporal = MagicMock()
        temporal.get_workflow_handle = MagicMock(return_value=handle)
        acts = _acts(temporal_mock=temporal)

        async def fake_get_task_result(tid: str) -> Any:
            schema = {"x-task-id": tid, "x-request-key": "approval"}
            await acts._elicitation_handler("Approve?", None, _FakeParams(schema), None)
            await asyncio.sleep(9999)  # server keeps running until the client cancels

        mcp = AsyncMock()
        mcp.get_task_result = fake_get_task_result
        acts._mcp = mcp

        result = await ActivityEnvironment().run(
            acts.handle_elicitation, SAMPLE_TASK_ID
        )
        assert result == "elicitation_handled"
        # both bookkeeping dicts cleared in finally
        assert SAMPLE_TASK_ID not in acts._active_elicitations
        assert SAMPLE_TASK_ID not in acts._elicitation_events

    async def test_returns_completed_when_no_elicitation(self):
        acts = _acts(temporal_mock=MagicMock())
        mcp = AsyncMock()
        mcp.get_task_result = AsyncMock(
            return_value={"content": [{"type": "text", "text": "PAID"}]}
        )
        acts._mcp = mcp
        result = await ActivityEnvironment().run(
            acts.handle_elicitation, SAMPLE_TASK_ID
        )
        assert result == "completed"
