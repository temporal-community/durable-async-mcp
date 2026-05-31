# ABOUTME: Tests for MCPActivities.
# All tests use ActivityEnvironment or set _active_elicitations directly.

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.client.elicitation import ElicitResult
from temporalio.testing import ActivityEnvironment

from async_mcp.client_worker.activities import MCPActivities
from async_mcp.client_worker.models import ElicitationDetails


SAMPLE_INVOICE = {"invoice_id": "INV-001", "customer": "Acme"}
SAMPLE_TASK_ID = "invoice-abc123"
SAMPLE_WF_ID = "task-tracker-wf-001"


def _make_activities(mcp_mock=None, temporal_mock=None) -> MCPActivities:
    return MCPActivities(
        mcp_client=mcp_mock or AsyncMock(),
        temporal_client=temporal_mock or AsyncMock(),
    )


class _FakeParams:
    """Minimal ElicitRequestParams substitute for testing."""

    def __init__(self, schema: dict) -> None:
        self.requestedSchema = schema


# ---------------------------------------------------------------------------
# start_task
# ---------------------------------------------------------------------------


class TestStartTask:
    async def test_returns_task_id(self):
        task_mock = MagicMock(task_id=SAMPLE_TASK_ID)
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(return_value=task_mock)

        acts = _make_activities(mcp_mock=mcp)
        env = ActivityEnvironment()
        result = await env.run(acts.start_task, SAMPLE_INVOICE)

        mcp.call_tool.assert_awaited_once_with(
            "process_invoice", {"invoice": SAMPLE_INVOICE}, task=True
        )
        assert result == SAMPLE_TASK_ID

    async def test_passes_invoice_json_as_invoice_key(self):
        task_mock = MagicMock(task_id="t-xyz")
        mcp = AsyncMock()
        mcp.call_tool = AsyncMock(return_value=task_mock)

        invoice = {"invoice_id": "INV-999", "customer": "Beta"}
        acts = _make_activities(mcp_mock=mcp)
        env = ActivityEnvironment()
        await env.run(acts.start_task, invoice)

        positional_args = mcp.call_tool.call_args.args
        assert positional_args[1] == {"invoice": invoice}


# ---------------------------------------------------------------------------
# poll_task_status
# ---------------------------------------------------------------------------


class TestPollTaskStatus:
    @pytest.mark.parametrize(
        "state",
        ["working", "input_required", "completed", "failed", "cancelled"],
    )
    async def test_returns_status_string(self, state: str):
        status_mock = MagicMock()
        status_mock.status = state

        mcp = AsyncMock()
        mcp.get_task_status = AsyncMock(return_value=status_mock)

        acts = _make_activities(mcp_mock=mcp)
        env = ActivityEnvironment()
        result = await env.run(acts.poll_task_status, SAMPLE_TASK_ID)

        assert result == state
        mcp.get_task_status.assert_awaited_once_with(SAMPLE_TASK_ID)


# ---------------------------------------------------------------------------
# get_task_result
# ---------------------------------------------------------------------------


class TestGetTaskResult:
    async def test_returns_text_content(self):
        raw = {"content": [{"type": "text", "text": "PAID"}], "isError": False}
        mcp = AsyncMock()
        mcp.get_task_result = AsyncMock(return_value=raw)

        acts = _make_activities(mcp_mock=mcp)
        env = ActivityEnvironment()
        result = await env.run(acts.get_task_result, SAMPLE_TASK_ID)

        assert "PAID" in result
        mcp.get_task_result.assert_awaited_once_with(SAMPLE_TASK_ID)

    async def test_joins_multiple_text_blocks(self):
        raw = {
            "content": [
                {"type": "text", "text": "Line 1"},
                {"type": "text", "text": "Line 2"},
            ],
            "isError": False,
        }
        mcp = AsyncMock()
        mcp.get_task_result = AsyncMock(return_value=raw)

        acts = _make_activities(mcp_mock=mcp)
        env = ActivityEnvironment()
        result = await env.run(acts.get_task_result, SAMPLE_TASK_ID)

        assert "Line 1" in result
        assert "Line 2" in result


# ---------------------------------------------------------------------------
# _elicitation_handler (reads x-task-id from schema, looks up _active_elicitations)
# ---------------------------------------------------------------------------


class TestElicitationHandler:
    def _make_with_active(self) -> MCPActivities:
        mock_handle = AsyncMock()
        mock_handle.query = AsyncMock(return_value="approve")
        temporal = MagicMock()
        temporal.get_workflow_handle = MagicMock(return_value=mock_handle)
        acts = _make_activities(temporal_mock=temporal)
        acts._active_elicitations[SAMPLE_TASK_ID] = SAMPLE_WF_ID
        return acts

    async def test_signals_workflow_with_elicitation_details(self):
        """Handler signals the workflow and returns ElicitResult."""
        acts = self._make_with_active()
        mock_handle = acts._temporal.get_workflow_handle.return_value

        schema = {
            "type": "object",
            "properties": {"value": {"type": "string", "enum": ["approve", "reject"]}},
            "x-task-id": SAMPLE_TASK_ID,
        }
        result = await acts._elicitation_handler(
            "Approve INV-001?", None, _FakeParams(schema), None
        )

        mock_handle.signal.assert_awaited_once()
        signal_args = mock_handle.signal.call_args.args
        assert signal_args[0] == "elicitation_received"
        details = signal_args[1]
        assert isinstance(details, ElicitationDetails)
        assert details.message == "Approve INV-001?"
        assert "x-task-id" not in details.schema

        assert isinstance(result, ElicitResult)
        assert result.action == "accept"
        assert result.content == {"value": "approve"}

    async def test_handler_sets_elicitation_event(self):
        """Handler sets the event before returning so handle_elicitation can cancel."""
        mock_handle = AsyncMock()
        mock_handle.query = AsyncMock(return_value="approve")
        temporal = MagicMock()
        temporal.get_workflow_handle = MagicMock(return_value=mock_handle)

        acts = _make_activities(temporal_mock=temporal)
        acts._active_elicitations[SAMPLE_TASK_ID] = SAMPLE_WF_ID
        event = asyncio.Event()
        acts._elicitation_events[SAMPLE_TASK_ID] = event

        schema = {"x-task-id": SAMPLE_TASK_ID}
        await acts._elicitation_handler("Approve?", None, _FakeParams(schema), None)

        assert event.is_set()

    async def test_handler_returns_decision_when_present(self):
        """Handler returns ElicitResult immediately when decision is already set."""
        mock_handle = AsyncMock()
        mock_handle.query = AsyncMock(return_value="reject")
        temporal = MagicMock()
        temporal.get_workflow_handle = MagicMock(return_value=mock_handle)
        acts = _make_activities(temporal_mock=temporal)
        acts._active_elicitations[SAMPLE_TASK_ID] = SAMPLE_WF_ID

        schema = {"x-task-id": SAMPLE_TASK_ID}
        result = await acts._elicitation_handler("Approve?", None, _FakeParams(schema), None)

        assert mock_handle.query.call_count == 1  # checks exactly once
        assert result.content == {"value": "reject"}

    async def test_handler_raises_when_no_decision(self):
        """Handler raises immediately when no decision is set, releasing the reader."""
        mock_handle = AsyncMock()
        mock_handle.query = AsyncMock(return_value=None)
        temporal = MagicMock()
        temporal.get_workflow_handle = MagicMock(return_value=mock_handle)
        acts = _make_activities(temporal_mock=temporal)
        acts._active_elicitations[SAMPLE_TASK_ID] = SAMPLE_WF_ID

        schema = {"x-task-id": SAMPLE_TASK_ID}
        with pytest.raises(RuntimeError, match="No decision yet"):
            await acts._elicitation_handler("Approve?", None, _FakeParams(schema), None)

        assert mock_handle.query.call_count == 1  # checks exactly once then gives up

    async def test_raises_when_no_task_id_in_schema(self):
        acts = _make_activities()
        with pytest.raises(RuntimeError, match="x-task-id"):
            await acts._elicitation_handler("Approve?", None, _FakeParams({}), None)

    async def test_raises_when_task_id_not_registered(self):
        acts = _make_activities()
        schema = {"x-task-id": "unknown-task"}
        with pytest.raises(RuntimeError, match="No active elicitation"):
            await acts._elicitation_handler("Approve?", None, _FakeParams(schema), None)


# ---------------------------------------------------------------------------
# handle_elicitation (uses ActivityEnvironment; verifies cancel-after-elicitation)
# ---------------------------------------------------------------------------


class TestHandleElicitation:
    async def test_returns_elicitation_handled_when_decision_present(self):
        """Returns 'elicitation_handled' and cancels tasks/result when decision found."""
        mock_handle = AsyncMock()
        # Decision is already set when handler checks
        mock_handle.query = AsyncMock(return_value="approve")
        mock_handle.signal = AsyncMock()
        temporal = MagicMock()
        temporal.get_workflow_handle = MagicMock(return_value=mock_handle)

        acts = _make_activities(temporal_mock=temporal)

        async def fake_get_task_result(tid: str) -> Any:
            schema = {"x-task-id": tid}
            await acts._elicitation_handler("Approve?", None, _FakeParams(schema), None)
            # Server keeps running after elicitation (waiting for workflow to finish)
            await asyncio.sleep(9999)
            return {"content": [{"type": "text", "text": "PAID"}], "isError": False}

        mcp = AsyncMock()
        mcp.get_task_result = fake_get_task_result
        acts._mcp = mcp

        env = ActivityEnvironment()
        result = await env.run(acts.handle_elicitation, SAMPLE_TASK_ID)

        assert result == "elicitation_handled"
        mock_handle.signal.assert_awaited()
        mock_handle.query.assert_awaited()

    async def test_registers_and_clears_both_dicts(self):
        """handle_elicitation registers task_id in both dicts and clears them in finally."""
        temporal = MagicMock()
        acts = _make_activities(temporal_mock=temporal)
        registered_elicitations: list[str] = []
        registered_events: list[str] = []

        async def fake_get_task_result(tid: str) -> Any:
            registered_elicitations.append(acts._active_elicitations.get(tid, ""))
            registered_events.append("yes" if tid in acts._elicitation_events else "no")
            return {"content": [{"type": "text", "text": "PAID"}], "isError": False}

        mcp = AsyncMock()
        mcp.get_task_result = fake_get_task_result
        acts._mcp = mcp

        env = ActivityEnvironment()
        await env.run(acts.handle_elicitation, SAMPLE_TASK_ID)

        assert registered_elicitations[0] != ""
        assert registered_events[0] == "yes"
        assert SAMPLE_TASK_ID not in acts._active_elicitations
        assert SAMPLE_TASK_ID not in acts._elicitation_events

    async def test_returns_completed_when_no_elicitation(self):
        """Returns 'completed' if get_task_result resolves without eliciting."""
        temporal = MagicMock()
        acts = _make_activities(temporal_mock=temporal)

        raw = {"content": [{"type": "text", "text": "PAID"}], "isError": False}
        mcp = AsyncMock()
        mcp.get_task_result = AsyncMock(return_value=raw)
        acts._mcp = mcp

        env = ActivityEnvironment()
        result = await env.run(acts.handle_elicitation, SAMPLE_TASK_ID)

        assert result == "completed"
