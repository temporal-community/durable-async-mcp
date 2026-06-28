# ABOUTME: Server-side wiring for the MCP Tasks protocol onto a FastMCP server, driven by a TaskBackend.
# register_tasks_extension overwrites FastMCP's task handlers (tasks/get/result/cancel + tools/call) with
# Temporal-backed ones: poll maps backend state, and input_required pushes elicitation during tasks/result.

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

import fastmcp.server.context
from mcp.shared.exceptions import McpError
from mcp.types import (
    INVALID_PARAMS,
    CallToolRequest,
    CallToolResult,
    CancelTaskRequest,
    CancelTaskResult,
    ErrorData,
    GetTaskPayloadRequest,
    GetTaskRequest,
    GetTaskResult,
    ServerResult,
    TextContent,
)

from mcp_tasks_temporal.backend import TaskBackend, TaskState

if TYPE_CHECKING:
    from fastmcp import FastMCP

# MCP task lifecycle states that are terminal (no further work or input).
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


def _require_task_id(params: Any) -> str:
    task_id = params.taskId
    if not task_id:
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message="Missing required parameter: taskId")
        )
    return str(task_id)


def _to_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value)


def _terminal_call_tool_result(state: TaskState) -> CallToolResult:
    """Build the CallToolResult a terminal task returns from tasks/result."""
    meta = {"modelcontextprotocol.io/related-task": {"taskId": state.task_id}}
    if state.status == "failed" or state.error is not None:
        message = (state.error or {}).get("message", "Task failed")
        return CallToolResult(
            content=[TextContent(type="text", text=message)], isError=True, _meta=meta
        )
    if state.result is not None:
        result = CallToolResult.model_validate(state.result)
        result.meta = {**(result.meta or {}), **meta}
        return result
    return CallToolResult(
        content=[TextContent(type="text", text=f"Task {state.status}")], _meta=meta
    )


def register_tasks_extension(
    mcp: FastMCP,
    backend: TaskBackend,
    *,
    task_tools: set[str] | None = None,
) -> None:
    """Wire the MCP Tasks protocol onto a FastMCP server, backed by `backend`.

    Overwrites FastMCP's (Docket-based) task handlers on the low-level server:
    - tools/call — for a task tool (any tool if `task_tools` is None), starts the durable job via
      `backend.start` and returns a task stub (`_meta` taskId + status). Non-task tools delegate
      to FastMCP's original handler.
    - tasks/get — maps `backend.get_state` to a GetTaskResult (status only).
    - tasks/result — when terminal, returns the result; when `input_required`, pushes an
      `elicitation/create` (the single pending inputRequest, tagged with `x-task-id`/`x-request-key`),
      applies the answer via `backend.submit_input`, then blocks on `backend.wait_result`.
    - tasks/cancel — `backend.cancel`.
    """
    low_level = mcp._mcp_server

    def _is_task_tool(name: str) -> bool:
        return task_tools is None or name in task_tools

    original_call_tool = low_level.request_handlers[CallToolRequest]

    async def on_call_tool(req: CallToolRequest) -> ServerResult:
        if not _is_task_tool(req.params.name):
            return await original_call_tool(req)
        state = await backend.start(req.params.name, req.params.arguments or {})
        return ServerResult(
            CallToolResult(
                content=[],
                _meta={
                    "modelcontextprotocol.io/task": {
                        "taskId": state.task_id,
                        "status": state.status,
                    }
                },
            )
        )

    async def on_tasks_get(req: GetTaskRequest) -> ServerResult:
        state = await backend.get_state(_require_task_id(req.params))
        return ServerResult(
            GetTaskResult(
                taskId=state.task_id,
                status=cast(Any, state.status),
                createdAt=_to_datetime(state.created_at),
                lastUpdatedAt=_to_datetime(state.last_updated_at),
                ttl=state.ttl_ms,
                pollInterval=state.poll_interval_ms,
                statusMessage=state.status_message,
            )
        )

    async def on_tasks_result(req: GetTaskPayloadRequest) -> ServerResult:
        task_id = _require_task_id(req.params)
        state = await backend.get_state(task_id)

        if state.status in TERMINAL_STATES:
            return ServerResult(_terminal_call_tool_result(state))

        if state.status == "input_required" and state.input_requests:
            key, request = next(iter(state.input_requests.items()))
            params = request.params
            # Embed routing hints so the client's elicitation handler can find the right
            # TaskTrackerWorkflow (x-task-id) and answer the right inputRequest (x-request-key).
            schema = {
                **params["requestedSchema"],
                "x-task-id": task_id,
                "x-request-key": key,
            }
            async with fastmcp.server.context.Context(fastmcp=mcp) as ctx:
                elicit_response = await ctx.session.elicit_form(
                    message=params["message"],
                    requestedSchema=schema,
                    related_request_id=ctx.request_id,
                )
            await backend.submit_input(
                task_id,
                {
                    key: {
                        "action": elicit_response.action,
                        "content": elicit_response.content,
                    }
                },
            )
            # Per spec tasks/result blocks until terminal; the client cancels this request after
            # the elicitation resolves and resumes polling, so this often does not return.
            result = await backend.wait_result(task_id)
            return ServerResult(CallToolResult.model_validate(result))

        raise McpError(
            ErrorData(
                code=INVALID_PARAMS,
                message=f"Task not completed yet (current state: {state.status})",
            )
        )

    async def on_tasks_cancel(req: CancelTaskRequest) -> ServerResult:
        task_id = _require_task_id(req.params)
        state = await backend.get_state(task_id)
        if state.status in TERMINAL_STATES:
            raise McpError(
                ErrorData(
                    code=INVALID_PARAMS,
                    message="Cannot cancel task: already in terminal status",
                )
            )
        await backend.cancel(task_id)
        return ServerResult(
            CancelTaskResult(
                taskId=task_id,
                status="cancelled",
                createdAt=_to_datetime(state.created_at),
                lastUpdatedAt=datetime.now(timezone.utc),
                ttl=state.ttl_ms,
                pollInterval=state.poll_interval_ms,
                statusMessage="Task cancelled",
            )
        )

    low_level.request_handlers[CallToolRequest] = on_call_tool
    low_level.request_handlers[GetTaskRequest] = on_tasks_get
    low_level.request_handlers[GetTaskPayloadRequest] = on_tasks_result
    low_level.request_handlers[CancelTaskRequest] = on_tasks_cancel
