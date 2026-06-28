# ABOUTME: TaskTrackerWorkflow — one durable workflow per in-flight MCP task; the client-side task handle.
# Polls tasks/get; on input_required drives server-push elicitation and awaits a human decision.

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from mcp_tasks_temporal.client import models

# Activities are referenced by string name (models.*), so this module imports no `mcp`/`fastmcp`
# and stays sandbox-clean. Only plain dataclasses cross the boundary.

_ACTIVITY_TIMEOUT = timedelta(seconds=30)


@workflow.defn
class TaskTrackerWorkflow:
    """Durable handle for a single MCP task. Survives client restarts: the task ID lives in
    workflow state and polling resumes on replay (no tasks/list needed).

    Internals use v1 server-push elicitation: when the task is `input_required`, the
    `handle_elicitation` activity opens tasks/result, the server pushes an `elicitation/create`,
    and the activity's handler signals `elicitation_received` (surfacing the inputRequests for the
    UI) then reads the human's `user_decision`. The external query/signal surface
    (`get_status`/`get_pending_input`/`user_decision`) matches the polling client so consumers
    (PurchaseOrderWorkflow, the UI, the GUI) are agnostic to the underlying protocol.
    """

    def __init__(self) -> None:
        self._task_id: str | None = None
        self._status: str = "working"
        # Outstanding inputRequests, keyed shape {key: {"method", "params": {message, requestedSchema}}}.
        self._pending_input: dict[str, Any] | None = None
        # The human's answer, keyed shape {key: {"action", "content"}}.
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
            self._status = await workflow.execute_activity(
                models.POLL_TASK,
                self._task_id,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                result_type=str,
            )

            if self._status in models.TERMINAL_STATUSES:
                result = await workflow.execute_activity(
                    models.GET_TASK_RESULT,
                    self._task_id,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    result_type=dict,
                )
                return {
                    "status": self._status,
                    "result": result,
                    "error": None,
                }

            if self._status == "input_required":
                # handle_elicitation drives tasks/result; its handler signals elicitation_received
                # (setting _pending_input) and reads user_decision. It retries indefinitely, each
                # attempt checking once for a decision then releasing the MCP reader, so concurrent
                # tasks aren't starved on the shared session.
                await workflow.execute_activity(
                    models.HANDLE_ELICITATION,
                    self._task_id,
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(
                        maximum_attempts=0,
                        maximum_interval=timedelta(seconds=10),
                    ),
                )
                # Reset for a possible next gate (e.g. cost-center after approval).
                self._decision = None
                self._pending_input = None
                # Brief pause before re-polling so we never spin if the activity returns fast.
                await workflow.sleep(timedelta(seconds=2))
                continue

            await workflow.sleep(
                timedelta(milliseconds=models.DEFAULT_POLL_INTERVAL_MS)
            )

    @workflow.signal
    def elicitation_received(self, input_requests: dict[str, Any]) -> None:
        """The activity's elicitation handler reports the server's pending inputRequests."""
        self._pending_input = input_requests

    @workflow.signal
    def user_decision(self, decision: dict[str, Any]) -> None:
        """UI supplies the inputResponses map answering the pending inputRequests."""
        self._decision = decision

    @workflow.query
    def get_pending_input(self) -> dict[str, Any] | None:
        """The outstanding inputRequests (for the UI to render), or None."""
        return self._pending_input

    @workflow.query
    def get_pending_decision(self) -> dict[str, Any] | None:
        """The human's decision (the activity's handler polls this), or None."""
        return self._decision

    @workflow.query
    def get_status(self) -> str:
        return self._status

    @workflow.query
    def get_task_id(self) -> str | None:
        return self._task_id
