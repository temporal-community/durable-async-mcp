# ABOUTME: TaskTrackerWorkflow — one instance per in-flight MCP task.
# Owns the full client-side task lifecycle: polling, elicitation, and result retrieval.

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from async_mcp.client_worker.models import ElicitationDetails, TaskTrackerInput

with workflow.unsafe.imports_passed_through():
    # Import activities class for execute_activity_method — passed through the
    # sandbox because the fastmcp import chain must not be determinism-checked.
    from async_mcp.client_worker.activities import MCPActivities


TERMINAL_STATES = {"completed", "failed", "cancelled"}
CLIENT_TASK_QUEUE = "client-task-queue"


@workflow.defn
class TaskTrackerWorkflow:
    """Durable client-side workflow that tracks one MCP task end-to-end.

    Polls the server for status, handles elicitation when human approval is
    needed, and retrieves the final result once the task is terminal.

    Signals:
        elicitation_received — activity calls this when the server elicits
        user_decision        — UI calls this when the human decides

    Queries:
        get_elicitation_details — UI reads this to display the approval prompt
        get_pending_decision    — activity polls this to detect the human's choice
    """

    def __init__(self) -> None:
        self._pending_decision: Optional[str] = None
        self._elicitation_details: Optional[ElicitationDetails] = None

    @workflow.signal
    async def elicitation_received(self, details: ElicitationDetails) -> None:
        self._elicitation_details = details

    @workflow.signal
    async def user_decision(self, decision: str) -> None:
        self._pending_decision = decision

    @workflow.query
    def get_elicitation_details(self) -> Optional[ElicitationDetails]:
        return self._elicitation_details

    @workflow.query
    def get_pending_decision(self) -> Optional[str]:
        return self._pending_decision

    @workflow.run
    async def run(self, input: TaskTrackerInput) -> str:
        # Phase 1: start a new MCP task or resume an existing one by task ID
        if input.task_id:
            task_id = input.task_id
        else:
            task_id = await workflow.execute_activity_method(
                MCPActivities.start_task,
                input.invoice_json,
                start_to_close_timeout=timedelta(seconds=30),
            )
            workflow.logger.info(f"Started MCP task: {task_id}")

        # Phase 2: poll + elicitation loop
        while True:
            status = await workflow.execute_activity_method(
                MCPActivities.poll_task_status,
                task_id,
                start_to_close_timeout=timedelta(seconds=30),
            )
            workflow.logger.info(f"Task {task_id} status: {status}")

            if status in TERMINAL_STATES:
                return await workflow.execute_activity_method(
                    MCPActivities.get_task_result,
                    task_id,
                    start_to_close_timeout=timedelta(seconds=30),
                )

            if status == "input_required":
                # Retries indefinitely. Each attempt checks for a decision once
                # (~20ms) then releases the MCP reader so other activities can run.
                # maximum_interval caps the backoff so the check frequency stays
                # reasonable while the human is deciding.
                await workflow.execute_activity_method(
                    MCPActivities.handle_elicitation,
                    task_id,
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(
                        maximum_attempts=0,
                        maximum_interval=timedelta(seconds=10),
                    ),
                )
                # Reset elicitation state; a future cycle could elicit again
                self._pending_decision = None
                self._elicitation_details = None
                # Brief pause before re-polling so we never spin if handle_elicitation
                # returns unexpectedly fast (e.g., activity raised and Temporal retried).
                await workflow.sleep(timedelta(seconds=2))
                continue

            await workflow.sleep(timedelta(seconds=2))
