# ABOUTME: Tests for TaskTrackerWorkflow.
# Uses the Temporal time-skipping test environment with mocked activities.

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from async_mcp.client_worker.models import ElicitationDetails, TaskTrackerInput
from async_mcp.client_worker.workflows import CLIENT_TASK_QUEUE, TaskTrackerWorkflow


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

SAMPLE_INVOICE = {
    "invoice_id": "INV-001",
    "customer": "Test Corp",
    "lines": [{"description": "Widget", "amount": 50.0, "due_date": "2025-01-01T00:00:00Z"}],
}


@asynccontextmanager
async def workflow_test_env(
    mock_start_task=None,
    mock_poll_task_status=None,
    mock_handle_elicitation=None,
    mock_get_task_result=None,
):
    """Start a Temporal test environment with mocked activities.

    Each mock defaults to a sensible stub if not provided.
    """
    task_id = "test-task-123"

    @activity.defn(name="start_task")
    async def start_task(invoice_json: dict) -> str:
        return mock_start_task(invoice_json) if mock_start_task else task_id

    @activity.defn(name="poll_task_status")
    async def poll_task_status(tid: str) -> str:
        return mock_poll_task_status(tid) if mock_poll_task_status else "completed"

    @activity.defn(name="handle_elicitation")
    async def handle_elicitation(tid: str) -> str:
        return mock_handle_elicitation(tid) if mock_handle_elicitation else "elicitation_handled"

    @activity.defn(name="get_task_result")
    async def get_task_result(tid: str) -> str:
        return mock_get_task_result(tid) if mock_get_task_result else "PAID"

    env = await WorkflowEnvironment.start_time_skipping()
    queue = f"test-queue-{uuid.uuid4()}"
    worker = Worker(
        env.client,
        task_queue=queue,
        workflows=[TaskTrackerWorkflow],
        activities=[start_task, poll_task_status, handle_elicitation, get_task_result],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "beartype", "fastmcp", "mcp"
            )
        ),
    )
    async with worker:
        yield env, queue
    await env.shutdown()


# ---------------------------------------------------------------------------
# Tests: task start / resume
# ---------------------------------------------------------------------------


class TestTaskStart:
    async def test_starts_new_task_when_no_task_id(self):
        """start_task activity is called with invoice_json when task_id is None."""
        calls = []

        def capture_start(invoice_json: dict) -> str:
            calls.append(invoice_json)
            return "task-abc"

        async with workflow_test_env(mock_start_task=capture_start) as (env, queue):
            handle = await env.client.start_workflow(
                TaskTrackerWorkflow.run,
                TaskTrackerInput(invoice_json=SAMPLE_INVOICE),
                id=f"wf-{uuid.uuid4()}",
                task_queue=queue,
            )
            result = await handle.result()

        assert calls == [SAMPLE_INVOICE]
        assert result == "PAID"

    async def test_skips_start_when_task_id_provided(self):
        """start_task is never called when a task_id is already supplied."""
        calls = []

        def should_not_be_called(invoice_json: dict) -> str:
            calls.append(invoice_json)
            return "should-not-run"

        async with workflow_test_env(mock_start_task=should_not_be_called) as (env, queue):
            handle = await env.client.start_workflow(
                TaskTrackerWorkflow.run,
                TaskTrackerInput(invoice_json=SAMPLE_INVOICE, task_id="existing-task"),
                id=f"wf-{uuid.uuid4()}",
                task_queue=queue,
            )
            await handle.result()

        assert calls == []


# ---------------------------------------------------------------------------
# Tests: polling loop
# ---------------------------------------------------------------------------


class TestPollingLoop:
    async def test_polls_until_completed(self):
        """Workflow keeps polling until status reaches 'completed'."""
        poll_responses = iter(["working", "working", "completed"])

        def poll(tid: str) -> str:
            return next(poll_responses)

        async with workflow_test_env(mock_poll_task_status=poll) as (env, queue):
            handle = await env.client.start_workflow(
                TaskTrackerWorkflow.run,
                TaskTrackerInput(invoice_json=SAMPLE_INVOICE, task_id="t1"),
                id=f"wf-{uuid.uuid4()}",
                task_queue=queue,
            )
            result = await handle.result()

        assert result == "PAID"

    async def test_polls_until_failed(self):
        """Workflow terminates on 'failed' status and returns get_task_result value."""
        poll_responses = iter(["working", "failed"])

        def poll(tid: str) -> str:
            return next(poll_responses)

        async with workflow_test_env(
            mock_poll_task_status=poll,
            mock_get_task_result=lambda tid: "FAILED",
        ) as (env, queue):
            handle = await env.client.start_workflow(
                TaskTrackerWorkflow.run,
                TaskTrackerInput(invoice_json=SAMPLE_INVOICE, task_id="t2"),
                id=f"wf-{uuid.uuid4()}",
                task_queue=queue,
            )
            result = await handle.result()

        assert result == "FAILED"

    async def test_handles_input_required_then_resumes(self):
        """Workflow calls handle_elicitation on input_required, then resumes polling."""
        poll_responses = iter(["input_required", "completed"])
        elicitation_calls = []

        def poll(tid: str) -> str:
            return next(poll_responses)

        def on_elicit(tid: str) -> str:
            elicitation_calls.append(tid)
            return "elicitation_handled"

        async with workflow_test_env(
            mock_poll_task_status=poll,
            mock_handle_elicitation=on_elicit,
        ) as (env, queue):
            handle = await env.client.start_workflow(
                TaskTrackerWorkflow.run,
                TaskTrackerInput(invoice_json=SAMPLE_INVOICE, task_id="t3"),
                id=f"wf-{uuid.uuid4()}",
                task_queue=queue,
            )
            result = await handle.result()

        assert elicitation_calls == ["t3"]
        assert result == "PAID"


# ---------------------------------------------------------------------------
# Tests: signals and queries
# ---------------------------------------------------------------------------


class TestSignalsAndQueries:
    async def test_elicitation_received_signal_sets_details(self):
        """Signalling elicitation_received stores the details for UI retrieval."""
        # Use a poll that pauses at input_required so signals can be sent
        poll_state = {"call": 0}

        def poll(tid: str) -> str:
            poll_state["call"] += 1
            if poll_state["call"] == 1:
                return "input_required"
            return "completed"

        async with workflow_test_env(mock_poll_task_status=poll) as (env, queue):
            details = ElicitationDetails(
                message="Approve invoice INV-001 for $50?",
                schema={"properties": {"decision": {"enum": ["approve", "reject"]}}},
            )
            handle = await env.client.start_workflow(
                TaskTrackerWorkflow.run,
                TaskTrackerInput(invoice_json=SAMPLE_INVOICE, task_id="t4"),
                id=f"wf-{uuid.uuid4()}",
                task_queue=queue,
            )
            await handle.signal(TaskTrackerWorkflow.elicitation_received, details)
            stored = await handle.query(TaskTrackerWorkflow.get_elicitation_details)

        assert stored is not None
        assert stored.message == details.message

    async def test_user_decision_signal_sets_pending_decision(self):
        """Signalling user_decision stores the decision for the activity to read."""
        poll_state = {"call": 0}

        def poll(tid: str) -> str:
            poll_state["call"] += 1
            if poll_state["call"] == 1:
                return "input_required"
            return "completed"

        async with workflow_test_env(mock_poll_task_status=poll) as (env, queue):
            handle = await env.client.start_workflow(
                TaskTrackerWorkflow.run,
                TaskTrackerInput(invoice_json=SAMPLE_INVOICE, task_id="t5"),
                id=f"wf-{uuid.uuid4()}",
                task_queue=queue,
            )
            await handle.signal(TaskTrackerWorkflow.user_decision, "approve")
            decision = await handle.query(TaskTrackerWorkflow.get_pending_decision)

        assert decision == "approve"

    async def test_elicitation_state_cleared_after_handled(self):
        """After elicitation is handled, pending_decision and details are cleared."""
        poll_responses = iter(["input_required", "completed"])

        def poll(tid: str) -> str:
            return next(poll_responses)

        async with workflow_test_env(mock_poll_task_status=poll) as (env, queue):
            handle = await env.client.start_workflow(
                TaskTrackerWorkflow.run,
                TaskTrackerInput(invoice_json=SAMPLE_INVOICE, task_id="t6"),
                id=f"wf-{uuid.uuid4()}",
                task_queue=queue,
            )
            # Send signals while the workflow is in elicitation phase
            details = ElicitationDetails(message="Approve?", schema={})
            await handle.signal(TaskTrackerWorkflow.elicitation_received, details)
            await handle.signal(TaskTrackerWorkflow.user_decision, "reject")

            # Wait for the workflow to complete; elicitation state should be cleared
            await handle.result()

            # After completion the state was cleared during the loop's reset
            pending = await handle.query(TaskTrackerWorkflow.get_pending_decision)
            elicit = await handle.query(TaskTrackerWorkflow.get_elicitation_details)

        assert pending is None
        assert elicit is None
