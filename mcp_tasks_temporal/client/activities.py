# ABOUTME: Temporal activities that drive the MCP tasks protocol over a shared client session.
# Generic over any task-augmented tool: start, poll (status + inputRequests), submit input, cancel.

from __future__ import annotations

from mcp.client.session import ClientSession
from temporalio import activity

from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.client.session import task_request


class MCPActivities:
    """Holds the shared MCP session (bound for the worker's lifetime) and the wire activities."""

    def __init__(self) -> None:
        self._session: ClientSession | None = None

    def bind(self, session: ClientSession | None) -> None:
        """Attach (or detach) the live session. Called by the plugin's run_context."""
        self._session = session

    @property
    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError(
                "MCP session is not bound; the tasks plugin run_context is not active"
            )
        return self._session

    @activity.defn(name=models.START_TASK)
    async def start_task(self, inp: models.StartTaskInput) -> str:
        res = await task_request(
            self._require_session,
            "tools/call",
            {"name": inp.tool_name, "arguments": inp.arguments},
        )
        return res["taskId"]

    @activity.defn(name=models.POLL_TASK)
    async def poll_task(self, task_id: str) -> models.TaskPollResult:
        res = await task_request(
            self._require_session, "tasks/get", {"taskId": task_id}
        )
        return models.TaskPollResult(
            status=res["status"],
            poll_interval_ms=res.get("pollIntervalMs"),
            input_requests=res.get("inputRequests"),
            result=res.get("result"),
            error=res.get("error"),
        )

    @activity.defn(name=models.SUBMIT_TASK_INPUT)
    async def submit_task_input(self, inp: models.SubmitInput) -> None:
        await task_request(
            self._require_session,
            "tasks/update",
            {"taskId": inp.task_id, "inputResponses": inp.input_responses},
        )

    @activity.defn(name=models.CANCEL_TASK)
    async def cancel_task(self, task_id: str) -> None:
        await task_request(self._require_session, "tasks/cancel", {"taskId": task_id})

    def activity_callables(self) -> list:
        """The bound activity methods, for worker/plugin registration."""
        return [
            self.start_task,
            self.poll_task,
            self.submit_task_input,
            self.cancel_task,
        ]
