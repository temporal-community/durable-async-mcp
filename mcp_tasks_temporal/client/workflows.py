# ABOUTME: TaskTrackerWorkflow — one durable workflow per in-flight MCP task; the client-side task handle.
# Polls tasks/get, surfaces inputRequests for a human, awaits a decision, submits via tasks/update.

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow

from mcp_tasks_temporal.client import models

# Activities are referenced by string name (models.*), so this module imports no `mcp` and stays
# sandbox-clean. Plain dataclasses cross the boundary.

_ACTIVITY_TIMEOUT = timedelta(seconds=30)


@workflow.defn
class TaskTrackerWorkflow:
    """Durable handle for a single MCP task. Survives client restarts: the task ID lives in
    workflow state and polling resumes on replay (no tasks/list needed)."""

    def __init__(self) -> None:
        self._task_id: str | None = None
        self._status: str = "working"
        self._pending_input: dict[str, Any] | None = None
        self._decision: dict[str, Any] | None = None

    @workflow.run
    async def run(self, inp: models.TaskTrackerInput) -> dict[str, Any]:
        self._task_id = await workflow.execute_activity(
            models.START_TASK,
            models.StartTaskInput(inp.tool_name, inp.arguments),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            result_type=str,
        )

        while True:
            poll: models.TaskPollResult = await workflow.execute_activity(
                models.POLL_TASK,
                self._task_id,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                result_type=models.TaskPollResult,
            )
            self._status = poll.status

            if poll.status in models.TERMINAL_STATUSES:
                return {
                    "status": poll.status,
                    "result": poll.result,
                    "error": poll.error,
                }

            if poll.status == "input_required":
                self._pending_input = poll.input_requests
                await workflow.wait_condition(lambda: self._decision is not None)
                await workflow.execute_activity(
                    models.SUBMIT_TASK_INPUT,
                    models.SubmitInput(self._task_id, self._decision or {}),
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                )
                self._decision = None
                self._pending_input = None

            interval_ms = poll.poll_interval_ms or models.DEFAULT_POLL_INTERVAL_MS
            await workflow.sleep(timedelta(milliseconds=interval_ms))

    @workflow.signal
    def user_decision(self, decision: dict[str, Any]) -> None:
        """UI supplies the inputResponses map answering the pending inputRequests."""
        self._decision = decision

    @workflow.query
    def get_pending_input(self) -> dict[str, Any] | None:
        """The outstanding inputRequests (for the UI to render), or None."""
        return self._pending_input

    @workflow.query
    def get_status(self) -> str:
        return self._status

    @workflow.query
    def get_task_id(self) -> str | None:
        return self._task_id
