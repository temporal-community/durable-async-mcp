# ABOUTME: Server-side wiring for the MCP Tasks extension onto an mcp.server.lowlevel.Server.
# register_tasks_extension installs the compat shim, advertises the capability, and wires tasks/*.

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp import types
from mcp.server.lowlevel.server import Server
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

from mcp_tasks_temporal._sdk_compat import install_tasks_result_passthrough
from mcp_tasks_temporal.backend import TaskBackend, TaskState
from mcp_tasks_temporal.wire import (
    EXTENSION_ID,
    CancelTaskRequestParams,
    CreateTaskResult,
    GetTaskRequestParams,
    GetTaskResult,
    UpdateTaskRequestParams,
    declares_tasks_extension,
)

CallToolHandler = Callable[[Any, types.CallToolRequestParams], Awaitable[Any]]


def _create_task_result(state: TaskState) -> dict[str, Any]:
    return CreateTaskResult(
        task_id=state.task_id,
        status=state.status,
        status_message=state.status_message,
        created_at=state.created_at,
        last_updated_at=state.last_updated_at,
        ttl_ms=state.ttl_ms,
        poll_interval_ms=state.poll_interval_ms,
    ).to_wire()


def _get_task_result(state: TaskState) -> dict[str, Any]:
    return GetTaskResult(
        task_id=state.task_id,
        status=state.status,
        status_message=state.status_message,
        created_at=state.created_at,
        last_updated_at=state.last_updated_at,
        ttl_ms=state.ttl_ms,
        poll_interval_ms=state.poll_interval_ms,
        input_requests=state.input_requests,
        result=state.result,
        error=state.error,
    ).to_wire()


def register_tasks_extension(
    server: Server,
    backend: TaskBackend,
    *,
    task_tools: set[str] | None = None,
    fallback_call_tool: CallToolHandler | None = None,
) -> None:
    """Wire the tasks extension onto a lowlevel Server.

    - Installs the alpha compat shim (so a bare CreateTaskResult survives tools/call validation).
    - Advertises `io.modelcontextprotocol/tasks` in server capabilities.
    - Wraps `tools/call`: for a task tool (any tool if `task_tools` is None), requires the client
      to have declared the extension in `_meta` (per spec MUST), then starts the job via `backend`
      and returns a CreateTaskResult. Non-task tools delegate to `fallback_call_tool` (or the
      server's existing tools/call handler).
    - Registers `tasks/get`, `tasks/update`, `tasks/cancel`.
    """
    install_tasks_result_passthrough()
    _advertise_capability(server)

    existing = server.get_request_handler("tools/call")
    fallback = fallback_call_tool or (existing.handler if existing else None)

    def _is_task_tool(name: str) -> bool:
        return task_tools is None or name in task_tools

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> Any:
        if _is_task_tool(params.name):
            if not declares_tasks_extension(params.meta):
                raise MCPError(
                    INVALID_PARAMS,
                    f"tool {params.name!r} requires the {EXTENSION_ID} extension; "
                    "declare it in params._meta clientCapabilities.",
                )
            state = await backend.start(params.name, params.arguments or {})
            return _create_task_result(state)
        if fallback is not None:
            return await fallback(ctx, params)
        raise MCPError(INVALID_PARAMS, f"unknown tool {params.name!r}")

    server.add_request_handler("tools/call", types.CallToolRequestParams, on_call_tool)

    async def on_tasks_get(ctx: Any, params: GetTaskRequestParams) -> dict[str, Any]:
        return _get_task_result(await backend.get_state(params.task_id))

    async def on_tasks_update(
        ctx: Any, params: UpdateTaskRequestParams
    ) -> dict[str, Any]:
        await backend.submit_input(params.task_id, params.input_responses)
        return {}  # empty ack

    async def on_tasks_cancel(
        ctx: Any, params: CancelTaskRequestParams
    ) -> dict[str, Any]:
        await backend.cancel(params.task_id)
        return {}  # empty ack

    server.add_request_handler("tasks/get", GetTaskRequestParams, on_tasks_get)
    server.add_request_handler("tasks/update", UpdateTaskRequestParams, on_tasks_update)
    server.add_request_handler("tasks/cancel", CancelTaskRequestParams, on_tasks_cancel)


def _advertise_capability(server: Server) -> None:
    """Add the tasks extension to the server's advertised capabilities."""
    orig_get_capabilities = server.get_capabilities

    def get_capabilities(
        notification_options: Any, experimental_capabilities: Any
    ) -> types.ServerCapabilities:
        caps = orig_get_capabilities(notification_options, experimental_capabilities)
        extensions = dict(getattr(caps, "extensions", None) or {})
        extensions[EXTENSION_ID] = {}
        caps.extensions = extensions
        return caps

    server.get_capabilities = get_capabilities  # type: ignore[method-assign]
