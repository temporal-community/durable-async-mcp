# ABOUTME: Temporal activities for managing MCP task lifecycle.
# All activities share a single fastmcp.Client connection held by MCPActivities.

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any, Optional

import mcp.types as mcp_types
from fastmcp.client.elicitation import ElicitResult
from temporalio import activity
from temporalio.client import Client as TemporalClient

from async_mcp.client_worker.models import ElicitationDetails

TERMINAL_TASK_STATES = {"completed", "failed", "cancelled"}


class MCPActivities:
    """Temporal activities that drive the MCP task protocol.

    Holds the shared fastmcp.Client for the lifetime of the worker.

    FastMCP dispatches incoming elicitation/create requests in a new asyncio
    task that does not inherit Temporal's context variables. To route each
    elicitation to the correct TaskTrackerWorkflow, handle_elicitation stores
    a mcp_task_id → tracker_workflow_id mapping in _active_elicitations before
    calling get_task_result. The server embeds mcp_task_id in the requestedSchema
    as "x-task-id" so _elicitation_handler can look up the right workflow.

    handle_elicitation uses asyncio.create_task + cancel-after-elicitation
    (per MCP spec) so connections are short-lived. This prevents long-lived
    concurrent connections from starving other activities on the shared client.
    """

    def __init__(
        self,
        mcp_client: Any,  # fastmcp.Client — typed as Any to avoid sandbox issues
        temporal_client: TemporalClient,
    ) -> None:
        self._mcp = mcp_client
        self._temporal = temporal_client
        # mcp_task_id -> TaskTrackerWorkflow_id, one entry per active elicitation.
        self._active_elicitations: dict[str, str] = {}
        # mcp_task_id -> asyncio.Event, set when the human decision arrives.
        self._elicitation_events: dict[str, asyncio.Event] = {}

    async def _elicitation_handler(
        self,
        message: str,
        response_type: Any,
        params: Any,
        context: Any,
    ) -> ElicitResult:
        """Called by FastMCP (in a new asyncio task) when the server elicits.

        Reads x-task-id from requestedSchema to look up the TaskTrackerWorkflow,
        signals it with the elicitation details, then polls for the human decision.
        Sets _elicitation_events[task_id] before returning so handle_elicitation
        can cancel the tasks/result connection per MCP spec.
        """
        schema = getattr(params, "requestedSchema", None) or {}
        task_id = schema.get("x-task-id")
        if not task_id:
            raise RuntimeError("Elicitation received without x-task-id in schema")

        workflow_id = self._active_elicitations.get(task_id)
        if not workflow_id:
            raise RuntimeError(f"No active elicitation registered for task {task_id}")

        display_schema = {k: v for k, v in schema.items() if k != "x-task-id"}
        details = ElicitationDetails(message=message, schema=display_schema)

        handle = self._temporal.get_workflow_handle(workflow_id)
        # Signal is idempotent — safe to re-send on every retry.
        await handle.signal("elicitation_received", details)

        # Check ONCE for a pending decision and return immediately either way.
        # The MCP session's receive loop processes incoming server→client requests
        # (like elicitation/create) inline — blocking the reader until the handler
        # returns. Polling here would starve other concurrent activities on the
        # shared client. Instead, we release the reader quickly: if no decision is
        # ready, raise so handle_elicitation fails and Temporal retries after backoff.
        # On each retry the server re-elicits and we check again — brief turns on
        # the reader rather than holding it indefinitely.
        decision = await handle.query("get_pending_decision")
        if decision is not None:
            # Set the event BEFORE returning so handle_elicitation's asyncio.wait
            # unblocks and cancels the tasks/result connection per MCP spec.
            event = self._elicitation_events.get(task_id)
            if event:
                event.set()
            return ElicitResult(action="accept", content={"value": decision})

        raise RuntimeError(
            f"No decision yet for task {task_id}; handle_elicitation will retry"
        )

    @activity.defn
    async def start_task(self, invoice_json: dict) -> str:
        """Start a new process_invoice MCP task and return the task ID."""
        task = await self._mcp.call_tool("process_invoice", {"invoice": invoice_json}, task=True)
        return task.task_id

    @activity.defn
    async def poll_task_status(self, task_id: str) -> str:
        """Query the MCP server for the current task state."""
        status_result = await self._mcp.get_task_status(task_id)
        return status_result.status

    @activity.defn
    async def handle_elicitation(self, task_id: str) -> str:
        """Drive tasks/result and cancel the connection after elicitation.

        Runs get_task_result in a background task so it can be cancelled once
        the human provides a decision. Per MCP spec, the client may cancel
        tasks/result after elicitation and resume polling — the server continues
        independently and the response is discarded.

        This keeps connections short-lived so concurrent tasks don't starve
        each other on the shared MCP client.
        """
        self._active_elicitations[task_id] = activity.info().workflow_id
        elicitation_resolved = asyncio.Event()
        self._elicitation_events[task_id] = elicitation_resolved

        try:
            result_task = asyncio.create_task(self._mcp.get_task_result(task_id))
            resolved_sentinel = asyncio.create_task(elicitation_resolved.wait())

            done, _ = await asyncio.wait(
                {result_task, resolved_sentinel},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if elicitation_resolved.is_set():
                # Per MCP spec: cancel tasks/result after elicitation, resume polling.
                result_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await result_task
                resolved_sentinel.cancel()
                return "elicitation_handled"
            else:
                # result_task finished first — no elicitation, or it raised.
                resolved_sentinel.cancel()
                result_task.result()  # re-raises any exception
                return "completed"
        finally:
            self._active_elicitations.pop(task_id, None)
            self._elicitation_events.pop(task_id, None)

    @activity.defn
    async def get_task_result(self, task_id: str) -> str:
        """Fetch the final result for a completed task and return it as text."""
        raw = await self._mcp.get_task_result(task_id)
        result = mcp_types.CallToolResult.model_validate(raw)
        parts = [block.text for block in result.content if hasattr(block, "text")]
        return "\n".join(parts) if parts else json.dumps(raw)
