# ABOUTME: The TaskBackend contract the server's tasks/* handlers call into, plus TaskState.
# A backend maps its own workflow/job state to MCP TaskState (e.g. Temporal-backed invoices).

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from mcp_tasks_temporal.wire import InputRequest, InputResponse, TaskStatus


@dataclass
class TaskState:
    """The MCP-facing state of a task — what the server renders onto the wire.

    Distinct from your underlying *workflow/job state*: the backend's job is to map
    workflow state -> TaskState (status plus the protocol payload). The server turns this
    into a CreateTaskResult (on start) or a GetTaskResult (on poll). Populate
    `input_requests` while `input_required`, `result` when `completed`, `error` when `failed`.
    """

    task_id: str
    status: TaskStatus
    created_at: str
    last_updated_at: str
    status_message: str | None = None
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None
    input_requests: dict[str, InputRequest] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@runtime_checkable
class TaskBackend(Protocol):
    """What an application plugs in to back the tasks extension.

    The server owns the MCP protocol; the backend owns the durable job (e.g. a Temporal
    workflow whose ID is the task ID) and maps its workflow state to MCP TaskState.
    """

    async def start(self, tool_name: str, arguments: dict[str, Any]) -> TaskState:
        """Durably start the job for a task-augmented tool call; return its initial TaskState."""
        ...

    async def get_state(self, task_id: str) -> TaskState:
        """Map the job's current workflow state to MCP TaskState (drives tasks/get)."""
        ...

    async def submit_input(
        self, task_id: str, input_responses: dict[str, InputResponse]
    ) -> None:
        """Apply the client's answers to outstanding inputRequests (drives tasks/update)."""
        ...

    async def cancel(self, task_id: str) -> None:
        """Cooperatively cancel the task (drives tasks/cancel)."""
        ...
