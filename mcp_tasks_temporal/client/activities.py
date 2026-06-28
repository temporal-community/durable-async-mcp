# ABOUTME: Temporal activities driving the MCP tasks protocol over a shared FastMCP client session.
# Generic over any task-augmented tool: start, poll status, handle server-push elicitation, fetch result.

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from fastmcp.client.elicitation import ElicitResult
from mcp.types import CallToolResult
from temporalio import activity
from temporalio.client import Client as TemporalClient

from mcp_tasks_temporal.client import models


class MCPActivities:
    """Holds the shared FastMCP client + a Temporal client, and the wire activities.

    FastMCP dispatches an incoming `elicitation/create` in a new asyncio task that has no Temporal
    activity context, so `handle_elicitation` records a task_id -> tracker-workflow-id mapping
    before opening tasks/result; the server embeds that task id (and the inputRequest key) in the
    `requestedSchema` so `_elicitation_handler` can signal/query the right TaskTrackerWorkflow.
    """

    def __init__(self, temporal_client: TemporalClient | None = None) -> None:
        self._mcp: Any = (
            None  # fastmcp.Client — typed Any to avoid importing it at module load
        )
        self._temporal = temporal_client
        # mcp_task_id -> TaskTrackerWorkflow id, one entry per active elicitation.
        self._active_elicitations: dict[str, str] = {}
        # mcp_task_id -> asyncio.Event, set when the human decision arrives.
        self._elicitation_events: dict[str, asyncio.Event] = {}

    def bind_mcp(self, mcp: Any) -> None:
        """Attach (or detach) the live FastMCP client. Called by the plugin's run_context."""
        self._mcp = mcp

    def bind_temporal(self, client: TemporalClient | None) -> None:
        self._temporal = client

    @property
    def _require_mcp(self) -> Any:
        if self._mcp is None:
            raise RuntimeError(
                "FastMCP client is not bound; the tasks plugin run_context is not active"
            )
        return self._mcp

    @property
    def _require_temporal(self) -> TemporalClient:
        if self._temporal is None:
            raise RuntimeError("Temporal client is not bound to MCPActivities")
        return self._temporal

    async def _elicitation_handler(
        self,
        message: str,
        response_type: Any,
        params: Any,
        context: Any,
    ) -> ElicitResult:
        """Called by FastMCP (in a new asyncio task) when the server elicits.

        Reads x-task-id + x-request-key from requestedSchema to route to the TaskTrackerWorkflow,
        signals it with the (keyed) inputRequests for the UI, then checks ONCE for a decision and
        returns immediately either way. If no decision is ready it raises, so handle_elicitation
        fails and Temporal retries after backoff — briefly releasing the MCP reader rather than
        holding it (which would starve other concurrent tasks on the shared client).
        """
        schema = getattr(params, "requestedSchema", None) or {}
        task_id = schema.get("x-task-id")
        key = schema.get("x-request-key")
        if not task_id or not key:
            raise RuntimeError(
                "Elicitation missing x-task-id / x-request-key in requestedSchema"
            )

        workflow_id = self._active_elicitations.get(task_id)
        if not workflow_id:
            raise RuntimeError(f"No active elicitation registered for task {task_id}")

        display_schema = {
            k: v for k, v in schema.items() if k not in ("x-task-id", "x-request-key")
        }
        input_requests = {
            key: {
                "method": "elicitation/create",
                "params": {"message": message, "requestedSchema": display_schema},
            }
        }

        handle = self._require_temporal.get_workflow_handle(workflow_id)
        # Idempotent — safe to re-send on every retry while the human is deciding.
        await handle.signal("elicitation_received", input_requests)

        decision = await handle.query("get_pending_decision")
        entry = decision.get(key) if decision else None
        if entry is not None:
            event = self._elicitation_events.get(task_id)
            if event:
                event.set()
            return ElicitResult(
                action=entry.get("action", "accept"), content=entry.get("content")
            )

        raise RuntimeError(
            f"No decision yet for task {task_id}; handle_elicitation will retry"
        )

    @activity.defn(name=models.START_TASK)
    async def start_task(self, inp: models.StartTaskInput) -> str:
        task = await self._require_mcp.call_tool(
            inp.tool_name, inp.arguments, task=True
        )
        return str(task.task_id)

    @activity.defn(name=models.POLL_TASK)
    async def poll_task(self, task_id: str) -> str:
        status_result = await self._require_mcp.get_task_status(task_id)
        return str(status_result.status)

    @activity.defn(name=models.HANDLE_ELICITATION)
    async def handle_elicitation(self, task_id: str) -> str:
        """Open tasks/result and cancel it once the human decides (per MCP spec).

        Runs get_task_result in a background task so it can be cancelled when the decision arrives;
        the server continues independently and its response is discarded. Keeps connections
        short-lived so concurrent tasks don't starve each other on the shared MCP client.
        """
        self._active_elicitations[task_id] = activity.info().workflow_id
        elicitation_resolved = asyncio.Event()
        self._elicitation_events[task_id] = elicitation_resolved

        try:
            result_task = asyncio.create_task(
                self._require_mcp.get_task_result(task_id)
            )
            resolved_sentinel = asyncio.create_task(elicitation_resolved.wait())

            await asyncio.wait(
                {result_task, resolved_sentinel},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if elicitation_resolved.is_set():
                result_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await result_task
                resolved_sentinel.cancel()
                return "elicitation_handled"
            else:
                resolved_sentinel.cancel()
                result_task.result()  # re-raise any exception from get_task_result
                return "completed"
        finally:
            self._active_elicitations.pop(task_id, None)
            self._elicitation_events.pop(task_id, None)

    @activity.defn(name=models.GET_TASK_RESULT)
    async def get_task_result(self, task_id: str) -> dict[str, Any]:
        raw = await self._require_mcp.get_task_result(task_id)
        result = (
            raw
            if isinstance(raw, CallToolResult)
            else CallToolResult.model_validate(raw)
        )
        return result.model_dump(mode="json", by_alias=True)

    def activity_callables(self) -> list:
        """The bound activity methods, for worker/plugin registration."""
        return [
            self.start_task,
            self.poll_task,
            self.handle_elicitation,
            self.get_task_result,
        ]
